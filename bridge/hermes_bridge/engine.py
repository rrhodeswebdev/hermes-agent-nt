"""Per-bar decision engine.

Wires store → indicators → agent → risk gate → command. The engine is pure with
respect to I/O (no HTTP, no NinjaTrader): it consumes bars/fills and returns
decisions and risk-approved commands. The server is responsible for queueing the
commands and shipping them to NinjaTrader. This keeps the engine fully testable
via the replay harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count

from .agent_client import AgentClient, AgentRequest, MockAgentClient
from .config import BridgeConfig, effective_entry_freshness_s, timeframe_seconds
from .indicators import MarketContext, build_context
from .journal import ClosedTrade, JournalStore, TradeTracker
from .models import Action, Bar, Decision, Fill, OrderCommand, Side
from .risk import RiskGate
from .session import SessionState
from .store import BarStore

_CONTEXT_WINDOW = 200  # bars handed to indicator/context building
_PLAN_TTL_CAP = 10     # max bars a plan may stay armed, whatever the agent asks for


@dataclass
class EngineResult:
    decision: Decision
    command: OrderCommand | None = None
    mode: str = ""
    risk_reasons: list[str] = field(default_factory=list)


@dataclass
class ArmedPlan:
    """A live entry plan: its ENTER_* limit command is resting in NinjaTrader."""

    cmd_id: str
    direction: Side
    entry_low: float
    entry_high: float
    bars_left: int
    note: str
    armed_ts: float


class TradingEngine:
    def __init__(
        self,
        config: BridgeConfig,
        store: BarStore,
        session: SessionState,
        agent: AgentClient,
        risk: RiskGate,
        journal: JournalStore | None = None,
        on_close: Callable[[ClosedTrade], None] | None = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.session = session
        self.agent = agent
        self.risk = risk
        self.journal = journal
        self.tracker = TradeTracker()
        self._pending_entry: dict | None = None
        self._ids = count(1)
        self.on_close = on_close
        self._prefilter = MockAgentClient(config) if config.agent.prefilter == "mock" else None
        self.last_context: MarketContext | None = None  # agent S/R + EMAs for the dashboard
        self.plan: ArmedPlan | None = None              # the one armed entry plan (or None)
        # Last Claude-DECLINED prefilter candidate: {action, price, ts}. Near-identical
        # candidates are answered from this memo instead of burning another Claude call
        # (extended trends produce the same candidate bar after bar). See _duplicate_decline.
        self._declined: dict | None = None

    def _new_id(self) -> str:
        return f"{self.cfg.strategy_id}-{next(self._ids)}"

    # ---- bar handling -------------------------------------------------------
    def on_bar(self, bar: Bar) -> EngineResult:
        self.session.maybe_roll_day(bar.ts)
        self.store.append(bar)
        self.session.mark_bar(bar.ts)
        if self.session.position != 0:
            self.tracker.on_bar(bar)

        # If the daily goal/limit was hit on a prior fill and we are still in a
        # position, flatten immediately regardless of what the agent thinks.
        if self.session.halted and self.session.position != 0:
            cmd = self.flatten_command(self.session.halt_reason or "halted")
            rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
            return EngineResult(
                Decision(action=Action.FLATTEN, rationale=cmd.reason),
                rd.command, "halt_flatten", rd.reasons,
            )

        bars = self.store.recent(_CONTEXT_WINDOW)
        ctx = build_context(
            bars,
            ema_fast=self.cfg.strategy.ema_fast,
            ema_slow=self.cfg.strategy.ema_slow,
            atr_period=self.cfg.strategy.atr_period,
        )
        self.last_context = ctx  # expose current S/R + EMAs to the dashboard
        account = self.session.account_state(mark_price=bar.close)
        mode = "manage_position" if self.session.position != 0 else "seek_entry"

        # An armed plan is managed DETERMINISTICALLY — no agent call, no prefilter.
        # (Must run before the halted early-return: a halt has to cancel the plan.)
        if mode == "seek_entry" and self.plan is not None:
            return self._manage_plan(bar)

        if mode == "seek_entry" and self.session.halted:
            return EngineResult(Decision(action=Action.WAIT, rationale="halted"), None, mode)

        candidate: Decision | None = None
        if self._prefilter is not None and mode == "seek_entry":
            pre = self._prefilter.decide(
                AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account))
            if pre.action not in (Action.ENTER_LONG, Action.ENTER_SHORT):
                return EngineResult(
                    Decision(action=Action.WAIT, rationale="prefilter:no_candidate"), None, mode)
            candidate = pre
            dup = self._duplicate_decline(candidate, ctx, bar)
            if dup is not None:
                return EngineResult(Decision(action=Action.WAIT, rationale=dup), None, mode)

        decision = self.agent.decide(
            AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account)
        )

        # Gate entry intents by minimum confidence (exits always honored).
        if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT, Action.ARM_PLAN):
            if decision.confidence < self.cfg.strategy.min_confidence:
                self._remember_decline(candidate, bar)
                return EngineResult(
                    Decision(action=Action.WAIT,
                             rationale=f"low_confidence:{decision.confidence}"),
                    None, mode,
                )

        if decision.action == Action.ARM_PLAN:
            return self._arm_plan(decision, bar, ctx, mode)

        if decision.action == Action.WAIT:
            self._remember_decline(candidate, bar)
            return EngineResult(decision, None, mode)

        self._declined = None  # an actionable decision invalidates the memo

        cmd = self._to_command(decision)
        rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
        if rd.approved and rd.command is not None and decision.action in (
            Action.ENTER_LONG, Action.ENTER_SHORT
        ):
            self._pending_entry = {
                "cmd_id": rd.command.id,
                "ts": bar.ts,
                "side": Side.LONG if decision.action == Action.ENTER_LONG else Side.SHORT,
                "context": ctx,
                "rationale": decision.rationale,
                "confidence": decision.confidence,
            }
        return EngineResult(decision, rd.command if rd.approved else None, mode, rd.reasons)

    # ---- armed-plan split -----------------------------------------------------
    def plan_snapshot(self) -> dict | None:
        """Armed-plan view for the dashboard (render_panel emits plan_* keys from it)."""
        p = self.plan
        if p is None:
            return None
        return {"status": "ARMED", "direction": str(p.direction),
                "entry_low": p.entry_low, "entry_high": p.entry_high,
                "bars_left": p.bars_left, "note": p.note}

    def _arm_plan(self, decision: Decision, bar: Bar, ctx: MarketContext,
                  mode: str) -> EngineResult:
        """Convert an ARM_PLAN decision into a risk-gated resting-limit ENTER command."""
        spec = decision.plan
        if not self.cfg.execution.armed_plans:
            return EngineResult(
                Decision(action=Action.WAIT, rationale="plans_disabled: arm ignored"),
                None, mode)
        if (spec is None or spec.direction not in (Side.LONG, Side.SHORT)
                or not spec.entry_low < spec.entry_high):
            return EngineResult(
                Decision(action=Action.WAIT, rationale="invalid_plan: arm ignored"),
                None, mode)
        action = Action.ENTER_LONG if spec.direction == Side.LONG else Action.ENTER_SHORT
        # The zone edge nearest current price: a long buys the dip INTO the zone, so
        # the limit rests at the top; a short sells the bounce, limit at the bottom.
        limit = spec.entry_high if spec.direction == Side.LONG else spec.entry_low
        ttl = max(1, min(_PLAN_TTL_CAP, spec.ttl_bars))
        cmd = OrderCommand(
            id=self._new_id(), strategy_id=self.cfg.strategy_id, action=action,
            qty=decision.qty if decision.qty > 0 else 1,
            stop_ticks=decision.stop_ticks, target_ticks=decision.target_ticks,
            stop_price=decision.stop_price, target_price=decision.target_price,
            limit_price=limit, reason=decision.rationale,
        )
        rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
        if not (rd.approved and rd.command is not None):
            return EngineResult(
                Decision(action=Action.WAIT,
                         rationale="plan_rejected:" + ",".join(rd.reasons)),
                None, mode, rd.reasons)
        self._declined = None  # an actionable arm invalidates the decline memo
        tf_s = timeframe_seconds(self.cfg.instrument.timeframe)
        self.plan = ArmedPlan(
            cmd_id=rd.command.id, direction=spec.direction,
            entry_low=spec.entry_low, entry_high=spec.entry_high,
            bars_left=ttl, note=spec.note, armed_ts=bar.ts,
        )
        # Journal memo lives as long as the plan can fill (TTL + one bar of slack),
        # not just the immediate freshness window.
        self._pending_entry = {
            "cmd_id": rd.command.id, "ts": bar.ts, "side": spec.direction,
            "context": ctx, "rationale": decision.rationale,
            "confidence": decision.confidence,
            "expires_ts": bar.ts + (ttl + 1) * tf_s,
        }
        return EngineResult(decision, rd.command, mode, rd.reasons)

    def _manage_plan(self, bar: Bar) -> EngineResult:
        """Per-bar plan upkeep: TTL countdown + deterministic invalidation."""
        p = self.plan
        if p is None:  # caller guards; defensive
            return EngineResult(Decision(action=Action.WAIT, rationale="no_plan"),
                                None, "plan_manage")
        p.bars_left -= 1
        reason: str | None = None
        if self.session.halted:
            reason = "halted"
        elif p.bars_left <= 0:
            reason = "ttl_expired"
        elif p.direction == Side.LONG and bar.close < p.entry_low:
            reason = "invalidated_close_below_zone"   # the pullback became a breakdown
        elif p.direction == Side.SHORT and bar.close > p.entry_high:
            reason = "invalidated_close_above_zone"
        if reason is None:
            return EngineResult(
                Decision(action=Action.WAIT, rationale=(
                    f"plan_armed:{p.direction} {p.entry_low:g}-{p.entry_high:g} "
                    f"({p.bars_left} bars left)")),
                None, "plan_manage")
        cmd = self._plan_cancel(reason)
        return EngineResult(
            Decision(action=Action.WAIT, rationale=f"plan_cancelled:{reason}"),
            cmd, "plan_manage")

    def _plan_cancel(self, reason: str) -> OrderCommand | None:
        """Clear the plan, disarm its journal memo, and emit CANCEL_ENTRY for NT8."""
        p = self.plan
        if p is not None:
            self.entry_dropped(p.cmd_id)
        self.plan = None
        cmd = OrderCommand(id=self._new_id(), strategy_id=self.cfg.strategy_id,
                           action=Action.CANCEL_ENTRY, qty=0, reason=f"plan:{reason}")
        rd = self.risk.evaluate(cmd, self.session)
        return rd.command if rd.approved else None

    def entry_dropped(self, cmd_id: str) -> None:
        """The server dropped this queued entry (stale): disarm the journal memo so the
        next fill — from any source — is not attributed to its context/rationale."""
        p = self._pending_entry
        if p is not None and p.get("cmd_id") == cmd_id:
            self._pending_entry = None

    def _matching_pending(self, side: Side, fill_ts: float) -> dict | None:
        """The armed entry memo, only if it plausibly produced this fill: same side and
        recent (decision budget + one bar). Anything else means the fill came from another
        source (manual, /agent/command, a dropped command that filled anyway) — journaling
        it under the memo's rationale would teach the reflector from a mislabeled trade."""
        p = self._pending_entry
        if p is None or p.get("side") != side:
            return None
        tf_s = timeframe_seconds(self.cfg.instrument.timeframe)
        deadline = p.get("expires_ts") or (
            float(p.get("ts", 0.0)) + effective_entry_freshness_s(self.cfg) + tf_s)
        if fill_ts > deadline:
            return None
        return p

    # ---- prefilter decline-dedup ---------------------------------------------
    def _remember_decline(self, candidate: Decision | None, bar: Bar) -> None:
        """Arm the dedup memo: Claude said no to this candidate at this price."""
        if candidate is not None:
            self._declined = {"action": candidate.action, "price": bar.close, "ts": bar.ts}

    def _duplicate_decline(self, candidate: Decision, ctx: MarketContext, bar: Bar) -> str | None:
        """Rationale string when this candidate is a near-duplicate of one Claude already
        declined (same direction, close within dedup_atr × ATR, within dedup_bars bars) —
        answered locally instead of re-asking. A direction flip clears the memo; a material
        price move or expiry lets Claude re-evaluate."""
        d = self._declined
        knobs = self.cfg.agent
        if d is None or knobs.prefilter_dedup_bars <= 0:
            return None
        tf_s = timeframe_seconds(self.cfg.instrument.timeframe) or 120
        bars_elapsed = int(max(0.0, bar.ts - d["ts"]) // tf_s)
        if bars_elapsed >= knobs.prefilter_dedup_bars:
            self._declined = None
            return None
        if candidate.action != d["action"]:
            self._declined = None
            return None
        atr = ctx.atr or 0.0
        if atr <= 0 or abs(bar.close - d["price"]) > knobs.prefilter_dedup_atr * atr:
            return None
        return (f"prefilter:duplicate_decline({d['action']} @{d['price']:g}, "
                f"bar {bars_elapsed + 1}/{knobs.prefilter_dedup_bars})")

    # ---- fill handling ------------------------------------------------------
    def on_fill(self, fill: Fill) -> OrderCommand | None:
        """Apply a fill, journal a completed trade on close, and flatten if the daily
        goal/limit tripped while still in a position."""
        before_pos = self.session.position
        before_pnl = self.session.realized_pnl
        self.session.apply_fill(fill)
        after_pos = self.session.position
        plan_was_armed = self.plan is not None

        if before_pos == 0 and after_pos != 0:
            side = Side.LONG if after_pos > 0 else Side.SHORT
            p = self._matching_pending(side, fill.ts)
            ctx = p["context"] if p is not None else self.last_context
            if ctx is not None:  # no context at all (fill before any bar): nothing to journal
                self.tracker.on_entry(
                    ts=fill.ts, side=side, qty=abs(after_pos), price=fill.price,
                    context=ctx,
                    rationale=p["rationale"] if p is not None
                    else "unattributed_fill (no matching pending entry)",
                    confidence=p.get("confidence", 0.0) if p is not None else 0.0,
                )
            self._pending_entry = None  # consumed or invalidated either way
            self.plan = None            # any new position consumes/invalidates the plan
        elif before_pos != 0 and after_pos == 0:
            trade = self.tracker.on_exit(
                ts=fill.ts, price=fill.price,
                realized_pnl=self.session.realized_pnl - before_pnl,
            )
            if trade is not None:
                if self.journal is not None:
                    self.journal.append(trade)
                if self.on_close is not None:
                    self.on_close(trade)

        reason = self.session.check_daily_goal()
        if reason and self.session.position != 0:
            cmd = self.flatten_command(reason)
            rd = self.risk.evaluate(cmd, self.session)
            return rd.command
        if plan_was_armed and before_pos == 0 and after_pos != 0:
            # Mop up any residual working entry quantity (partial-fill edge): the
            # strategy's CANCEL_ENTRY is a no-op when the order is already done.
            cancel = OrderCommand(id=self._new_id(), strategy_id=self.cfg.strategy_id,
                                  action=Action.CANCEL_ENTRY, qty=0,
                                  reason="plan:entry_filled")
            rd = self.risk.evaluate(cancel, self.session)
            return rd.command if rd.approved else None
        return None

    # ---- helpers ------------------------------------------------------------
    def flatten_command(self, reason: str) -> OrderCommand:
        return OrderCommand(
            id=self._new_id(), strategy_id=self.cfg.strategy_id,
            action=Action.FLATTEN, qty=abs(self.session.position), reason=reason,
        )

    def _to_command(self, d: Decision) -> OrderCommand:
        return OrderCommand(
            id=self._new_id(),
            strategy_id=self.cfg.strategy_id,
            action=d.action,
            qty=d.qty if d.qty > 0 else 1,
            stop_ticks=d.stop_ticks,
            target_ticks=d.target_ticks,
            stop_price=d.stop_price,
            target_price=d.target_price,
            reason=d.rationale,
        )
