"""RiskGate — the single safety chokepoint.

EVERY order command (from the deterministic engine, the LLM agent's `nt_place_order`
tool, or a manual API call) is evaluated here before it can be queued for
NinjaTrader. The gate is pure and fully unit-tested. Rules:

  * EXIT / FLATTEN are ALWAYS allowed (we must always be able to reduce risk).
  * Entries are rejected when the session is halted or the daily goal is hit.
  * Entries require a flat position (no pyramiding/flips in v1).
  * Entries respect max trades/day and the position cap (qty is clamped down).
  * Every entry must carry a protective stop; a default is injected if missing.
  * The protective stop is CLAMPED into the configured tick band (min/max_stop_ticks):
    a vol-noise floor so it can't be razor-thin, a spike ceiling so it can't run away.
  * Per-trade dollar risk must not exceed max_risk_per_trade — itself scaled down by
    ``risk_scale`` in a volatility shock (qty clamped to fit the scaled budget).
  * An entry whose worst-case stop-out would breach max_daily_loss is rejected.
  * Optional trading-hours window can reject entries outside RTH.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import BridgeConfig
from .models import Action, OrderCommand
from .session import SessionState
from .stops import clamp_stop_ticks, size_for_confidence, vol_stop_floor_ticks


@dataclass
class RiskDecision:
    approved: bool
    command: OrderCommand | None
    reasons: list[str] = field(default_factory=list)


_ENTRIES = {Action.ENTER_LONG, Action.ENTER_SHORT}


class RiskGate:
    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config

    def evaluate(
        self,
        command: OrderCommand,
        session: SessionState,
        *,
        last_price: float | None = None,
        now_ts: float | None = None,
        risk_scale: float = 1.0,
        confidence: float | None = None,
        atr: float | None = None,
    ) -> RiskDecision:
        # Risk-reducing actions are never blocked.
        if command.action in (Action.EXIT, Action.FLATTEN):
            qty = abs(session.position) if command.qty <= 0 else command.qty
            return RiskDecision(True, command.model_copy(update={"qty": qty}), ["risk_reducing"])

        if command.action not in _ENTRIES:
            return RiskDecision(False, None, [f"unsupported_action:{command.action}"])

        reasons: list[str] = []

        # 1) Halt / daily goal.
        if session.halted:
            return RiskDecision(False, None, [f"halted:{session.halt_reason or 'halted'}"])
        if session.daily_goal_hit:
            return RiskDecision(False, None, ["daily_goal_hit"])

        # 2) Trading-hours window (optional).
        if self.cfg.session.enforce_hours and now_ts is not None:
            if not self._within_hours(now_ts):
                return RiskDecision(False, None, ["outside_session_hours"])

        # 3) Flat-only entries.
        if session.position != 0:
            return RiskDecision(False, None, ["already_in_position"])

        # 4) Max trades/day.
        if session.trades_today >= self.cfg.risk.max_trades_per_day:
            return RiskDecision(False, None, ["max_trades_per_day"])

        # 5) Base/requested size and the position cap. Final sizing happens in step 8,
        # once the per-trade dollar budget (step 7) is known.
        requested = command.qty if command.qty > 0 else 1
        cap = self.cfg.risk.max_contracts

        # 6) Mandatory protective stop (inject default if missing), clamped to the band.
        stop_ticks = command.stop_ticks
        stop_price = command.stop_price
        if stop_ticks is None and stop_price is None:
            stop_ticks = self.cfg.risk.default_stop_ticks
            reasons.append(f"default_stop_injected:{stop_ticks}")
        # The band is the final word on stop size regardless of source (rules brain, the
        # Claude brain's nudge, or the injected default): a vol-noise floor and a spike
        # ceiling. The floor is volatility-scaled (min_stop_atr_mult × ATR) when ATR is known,
        # so a brain that proposes a razor-thin stop in a high-ATR market is widened to give
        # the trade room. A price stop is widened/capped by adjusting the price to the band.
        vol_floor = vol_stop_floor_ticks(atr, self.cfg)
        if stop_ticks is not None:
            clamped = clamp_stop_ticks(stop_ticks, self.cfg, floor_ticks=vol_floor)
            if clamped != stop_ticks:
                reasons.append(f"stop_band_clamped:{stop_ticks}->{clamped}")
            stop_ticks = clamped
        elif stop_price is not None and last_price is not None:
            stop_price = self._band_clamp_price(
                stop_price, command.action, last_price, reasons, floor_ticks=vol_floor
            )

        # 7) Per-trade dollar risk (clamp qty down to fit). The budget itself shrinks in a
        # volatility shock via risk_scale, so wild conditions get smaller size.
        risk_ticks = self._risk_ticks(stop_ticks, stop_price, command.action, last_price)
        if risk_ticks is None or risk_ticks <= 0:
            return RiskDecision(False, None, ["cannot_determine_stop_distance"])
        per_contract_risk = risk_ticks * self.cfg.instrument.tick_value
        effective_max_risk = self.cfg.risk.max_risk_per_trade * max(0.0, risk_scale)
        if risk_scale < 1.0:
            reasons.append(f"risk_scaled:{risk_scale:g}")
        max_qty_by_risk = int(effective_max_risk // per_contract_risk)
        if max_qty_by_risk < 1:
            return RiskDecision(
                False, None, [f"single_contract_risk_exceeds_max:{per_contract_risk:.2f}"]
            )

        # 8) Final size. budget_max = the most contracts BOTH the position cap and the
        # dollar budget allow. With confidence_sizing on, scale UP with the decision's
        # confidence (1 at min_confidence → full budget at full_size_confidence); otherwise
        # take the requested qty clamped DOWN to the budget (legacy behavior).
        budget_max = min(cap, max_qty_by_risk)
        if self.cfg.risk.confidence_sizing:
            # Always confidence-size when enabled — a MISSING confidence must not fall through
            # to the legacy requested-qty path (that let a manual/no-confidence entry size up to
            # the full budget, bypassing the gate). size_for_confidence() returns the 1-contract
            # minimum when confidence is None, so that conservative floor is enforced server-side
            # for every entry path (engine, manual API, agent).
            qty = size_for_confidence(
                confidence, budget_max,
                self.cfg.strategy.min_confidence, self.cfg.risk.full_size_confidence,
            )
            if qty >= 1:
                conf_str = f"{confidence:g}" if confidence is not None else "none"
                reasons.append(f"confidence_sized:{conf_str}->{qty}")
        else:
            qty = min(requested, budget_max)
            if requested > cap:
                reasons.append(f"qty_clamped_to_cap:{cap}")
            elif requested > max_qty_by_risk:
                reasons.append(f"qty_clamped_by_risk:{qty}")
        if qty < 1:
            return RiskDecision(False, None, ["zero_qty"])

        trade_risk = per_contract_risk * qty

        # 9) Daily-loss projection: would the worst case breach the daily loss cap?
        projected_worst = session.realized_pnl - trade_risk
        if projected_worst <= -session.max_daily_loss:
            return RiskDecision(
                False,
                None,
                [f"would_breach_daily_loss:realized={session.realized_pnl:.2f}"
                 f",risk={trade_risk:.2f},limit={session.max_daily_loss:.2f}"],
            )

        # Preserve the intended reward:risk. When the stop was widened to the vol floor, a
        # brain that paired it with a tight target would be left with a WIDE stop and a TINY
        # target — an inverted R:R (small wins, big losses). Widen the target to keep at least
        # the configured atr_target_mult/atr_stop_mult ratio. ``risk_ticks`` is the POST-clamp
        # stop distance regardless of whether the stop arrived as ticks or as a (now band-clamped)
        # price, so a price-stop + ticks-target bracket gets the same protection. Only the ticks
        # target is scaled (the common path); an explicit price target is left to the brain.
        target_ticks = command.target_ticks
        if (vol_floor is not None and target_ticks is not None
                and self.cfg.strategy.atr_stop_mult > 0):
            rr = self.cfg.strategy.atr_target_mult / self.cfg.strategy.atr_stop_mult
            min_target = round(risk_ticks * rr)
            if target_ticks < min_target:
                reasons.append(f"target_widened_to_rr:{target_ticks}->{min_target}")
                target_ticks = min_target

        approved = command.model_copy(
            update={
                "qty": qty,
                "stop_ticks": stop_ticks if stop_price is None else None,
                "stop_price": stop_price,
                "target_ticks": target_ticks,
            }
        )
        reasons.append(f"approved:risk={trade_risk:.2f}")
        return RiskDecision(True, approved, reasons)

    # ---- helpers ------------------------------------------------------------
    def _band_clamp_price(
        self, stop_price: float, action: Action, last_price: float, reasons: list[str],
        floor_ticks: int | None = None,
    ) -> float:
        """Pull a price stop into the tick band by adjusting its DISTANCE from the entry:
        widen a too-tight stop to the floor, cap a too-wide one at the ceiling. The floor
        includes the volatility floor (``floor_ticks``) when supplied. Direction is honored
        (a long's stop sits below entry, a short's above)."""
        tick = self.cfg.instrument.tick_size or 0.25
        dist_ticks = abs(last_price - stop_price) / tick
        clamped = clamp_stop_ticks(round(dist_ticks), self.cfg, floor_ticks=floor_ticks)
        if abs(clamped - dist_ticks) < 1e-9:
            return stop_price
        new_price = (
            last_price - clamped * tick
            if action == Action.ENTER_LONG
            else last_price + clamped * tick
        )
        reasons.append(f"stop_band_clamped:{stop_price:g}->{new_price:g}")
        return new_price

    def _risk_ticks(
        self,
        stop_ticks: int | None,
        stop_price: float | None,
        action: Action,
        last_price: float | None,
    ) -> float | None:
        if stop_ticks is not None:
            return float(stop_ticks)
        if stop_price is not None and last_price is not None:
            dist = abs(last_price - stop_price)
            return dist / self.cfg.instrument.tick_size if self.cfg.instrument.tick_size else None
        return None

    def _within_hours(self, now_ts: float) -> bool:
        tz = ZoneInfo(self.cfg.session.timezone)
        local = datetime.fromtimestamp(now_ts, tz).time()
        start = _parse_hhmm(self.cfg.session.start)
        end = _parse_hhmm(self.cfg.session.end)
        return start <= local <= end


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))
