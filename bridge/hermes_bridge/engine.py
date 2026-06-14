"""Per-bar decision engine.

Wires store → indicators → agent → risk gate → command. The engine is pure with
respect to I/O (no HTTP, no NinjaTrader): it consumes bars/fills and returns
decisions and risk-approved commands. The server is responsible for queueing the
commands and shipping them to NinjaTrader. This keeps the engine fully testable
via the replay harness.

With a Planner attached, the LLM never sits on the bar-close critical path: each
close is answered instantly from the plan armed by the PREVIOUS between-bars
analysis, and the follow-up analysis for the next close is scheduled afterwards.
Without one, the legacy per-bar `agent.decide()` call is used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count

from .agent_client import AgentClient, AgentRequest, MockAgentClient
from .config import BridgeConfig, effective_entry_freshness_s, timeframe_seconds
from .indicators import MarketContext, atr, build_context
from .journal import ClosedTrade, JournalStore, TradeTracker
from .levels import detect_levels
from .models import (
    AccountState,
    Action,
    Bar,
    Decision,
    Fill,
    Level,
    Mode,
    OrderCommand,
    Side,
)
from .plan import Planner, PlanRequest, TradePlan, evaluate_plan
from .risk import RiskGate
from .session import SessionState
from .store import BarStore

_CONTEXT_WINDOW = 200  # bars handed to indicator/context building


def is_volatility_shock(
    cur_atr: float | None, baseline_atr: float | None, shock_ratio: float
) -> bool:
    """An extreme volatility shift: current ATR is ``shock_ratio``× the baseline (a spike)
    or ≤ 1/``shock_ratio`` of it (a collapse). Either way the regime read likely changed."""
    if not cur_atr or not baseline_atr or baseline_atr <= 0 or shock_ratio <= 1:
        return False
    r = cur_atr / baseline_atr
    return r >= shock_ratio or r <= 1.0 / shock_ratio


@dataclass
class EngineResult:
    decision: Decision
    command: OrderCommand | None = None
    mode: str = ""
    risk_reasons: list[str] = field(default_factory=list)


class TradingEngine:
    def __init__(
        self,
        config: BridgeConfig,
        store: BarStore,
        session: SessionState,
        agent: AgentClient,
        risk: RiskGate,
        planner: Planner | None = None,
        journal: JournalStore | None = None,
        on_close: Callable[[ClosedTrade], None] | None = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.session = session
        self.agent = agent
        self.risk = risk
        self.planner = planner if config.planner.enabled else None
        self.journal = journal
        self.tracker = TradeTracker()
        self._pending_entry: dict | None = None
        self._ids = count(1)
        self.on_close = on_close
        self._prefilter = MockAgentClient(config) if config.agent.prefilter == "mock" else None
        self.last_context: MarketContext | None = None  # agent regime / S/R for the dashboard
        # Re-author governor state (agent mode; see _maybe_reauthor). _bars_since_author drives
        # the debounce floor / freshness ceiling / failed-author retry; _struct_change_bars
        # counts consecutive closes the live structure has diverged from the authored playbook
        # (reset the moment it fits again); the _authored_* anchor records the regime/trend the
        # live playbook was authored under, so a flip away from it is detectable.
        self._bars_since_author = 0
        self._struct_change_bars = 0
        self._authored_regime: str | None = None
        self._authored_trend: str | None = None
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
            atr_period=self.cfg.strategy.atr_period,
            swing_lookback=self.cfg.strategy.swing_lookback,
        )
        self.last_context = ctx  # expose current regime / S/R to the dashboard
        self._maybe_reauthor(ctx)  # volatility-adaptive playbook refresh (agent mode)
        account = self.session.account_state(mark_price=bar.close)
        mode = "manage_position" if self.session.position != 0 else "seek_entry"

        if mode == "seek_entry" and self.session.halted:
            return EngineResult(Decision(action=Action.WAIT, rationale="halted"), None, mode)

        armed = self.planner.current_plan() if self.planner is not None else None
        candidate: Decision | None = None
        if self.planner is not None:
            # Plan cycle: answer the close instantly from the plan the previous
            # between-bars analysis armed. The prefilter does not apply here — Claude
            # already ran off the critical path.
            decision = self._evaluate_armed_plan(armed, bar, mode)
        else:
            if self._prefilter is not None and mode == "seek_entry":
                pre = self._prefilter.decide(
                    AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account))
                if pre.action not in (Action.ENTER_LONG, Action.ENTER_SHORT):
                    return EngineResult(
                        Decision(action=Action.WAIT, rationale="prefilter:no_candidate"),
                        None, mode)
                candidate = pre
                dup = self._duplicate_decline(candidate, ctx, bar)
                if dup is not None:
                    return EngineResult(Decision(action=Action.WAIT, rationale=dup), None, mode)
            decision = self.agent.decide(
                AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account)
            )

        # Gate entries by minimum confidence (exits always honored).
        if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            if decision.confidence < self.cfg.strategy.min_confidence:
                decision = Decision(action=Action.WAIT,
                                    rationale=f"low_confidence:{decision.confidence}")

        if decision.action == Action.WAIT:
            self._remember_decline(candidate, bar)
            result = EngineResult(decision, None, mode)
        else:
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
            result = EngineResult(decision, rd.command if rd.approved else None, mode,
                                  rd.reasons)
            if armed is not None and result.command is not None:
                # The armed plan produced a queued order: a plan fires at most once,
                # even if the fill (and the re-arming analysis) is still in flight.
                self.planner.consume(armed)
        # Schedule the between-bars analysis AFTER the instant answer is known, so
        # the next plan can assume the optimistic post-fill position of anything
        # queued this close. With a synchronous planner this arms before we return.
        if self.planner is not None:
            self._schedule_followup(bar, ctx, bars, account, result)
        return result

    # ---- pre-armed plan cycle -------------------------------------------------
    def _evaluate_armed_plan(self, plan: TradePlan | None, bar: Bar, mode: Mode) -> Decision:
        if plan is None:
            return Decision(action=Action.WAIT, rationale="no_plan (analysis pending)")
        if plan.based_on_bar_ts >= bar.ts:
            # The plan was made from this very bar (or newer); it can only apply to
            # closes that happen after its basis.
            return Decision(action=Action.WAIT, rationale="plan_not_yet_active")
        if self._plan_is_stale(plan):
            return Decision(
                action=Action.WAIT,
                rationale=f"plan_stale (basis_ts={plan.based_on_bar_ts:g}, "
                          f"max_age={self.cfg.planner.max_plan_age_bars} bars)",
            )
        if plan.mode != mode:
            return Decision(
                action=Action.WAIT,
                rationale=f"plan_mode_mismatch (armed={plan.mode}, actual={mode})",
            )
        return evaluate_plan(plan, bar, self.session.position)

    def _plan_is_stale(self, plan: TradePlan) -> bool:
        # Dead once the basis bar is max_plan_age_bars closes old — i.e. it has
        # scrolled out of the last max_age closes. Matches the config promise: "a
        # plan based on a bar this many closes old no longer fires".
        max_age = self.cfg.planner.max_plan_age_bars
        recent = self.store.recent(max_age)
        return len(recent) >= max_age and all(
            b.ts > plan.based_on_bar_ts for b in recent
        )

    def _schedule_followup(self, bar: Bar, ctx: MarketContext, bars: list[Bar],
                           account: AccountState, result: EngineResult) -> None:
        cmd = result.command
        if cmd is not None and cmd.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            assumed = cmd.qty if cmd.action is Action.ENTER_LONG else -cmd.qty
        elif cmd is not None and cmd.action in (Action.EXIT, Action.FLATTEN):
            assumed = 0
        else:
            assumed = self.session.position
        next_mode: Mode = "manage_position" if assumed != 0 else "seek_entry"
        queued = f" queued={cmd.action}:{cmd.qty}" if cmd is not None else ""
        outcome = f"{result.decision.action}: {result.decision.rationale}{queued}"
        self.planner.schedule_plan_analysis(PlanRequest(
            mode=next_mode, context=ctx, recent_bars=bars, account=account,
            bar_ts=bar.ts, assumed_position=assumed, levels=self._levels(bars),
            outcome=outcome,
        ))

    def on_history(self, bars: list[Bar]) -> None:
        """Kick off the one-time session study (and the initial plan) after a
        history bulk-load. No-op without a planner."""
        self._trigger_session_study(bars, force=False, outcome="session_start")

    def _trigger_session_study(self, bars: list[Bar], *, force: bool, outcome: str) -> None:
        """Schedule the pre-session study (authors the playbook in agent mode) from ``bars``
        and reset the re-author clock. ``force`` re-runs even when a brief already exists
        (the volatility-adaptive re-author); the study overwrites the playbook in place."""
        if self.planner is None or not bars:
            return
        recent = bars[-_CONTEXT_WINDOW:]
        ctx = build_context(
            recent,
            atr_period=self.cfg.strategy.atr_period,
            swing_lookback=self.cfg.strategy.swing_lookback,
        )
        account = self.session.account_state(mark_price=bars[-1].close)
        mode: Mode = "manage_position" if self.session.position != 0 else "seek_entry"
        self._bars_since_author = 0
        self._struct_change_bars = 0
        # Anchor the structural staleness check to what this study authors from, so the next
        # re-author fires when the live market drifts off THIS read (not the previous one).
        self._authored_regime = ctx.regime
        self._authored_trend = ctx.trend
        self.planner.schedule_session_analysis(bars, PlanRequest(
            mode=mode, context=ctx, recent_bars=recent, account=account,
            bar_ts=bars[-1].ts, assumed_position=self.session.position,
            levels=self._levels(recent), outcome=outcome,
        ), force=force)

    def _maybe_reauthor(self, ctx: MarketContext) -> None:
        """Structure-driven re-author (agent mode): refresh the playbook when the live market
        no longer matches the one the brain authored — the trend flipped against it, or no
        authored setup covers the live regime — confirmed over ``confirm_bars`` closes so a
        one-bar wobble doesn't thrash it. A volatility shock (mis-scaled brackets) and a
        freshness ceiling are secondary triggers, and a failed author is retried rather than
        left in WAIT. The old playbook keeps trading until the new one lands (no WAIT gap)."""
        rc = self.cfg.strategies.reauthor
        if (not rc.enabled or self.planner is None
                or self.agent.strategy_source() != "agent"
                or self.planner.is_analyzing_session()):          # one already in flight
            return
        self._bars_since_author += 1

        # A failed/empty author left no playbook to trade: retry on a short clock instead of
        # sitting in WAIT forever (the old governor skipped here and never recovered).
        if self.agent.generated_strategy() is None:
            if self._bars_since_author >= rc.retry_bars:
                self._reauthor_now(ctx, "author_retry")
            return

        # How long the live structure has been at odds with the authored playbook.
        stale = self._playbook_stale(ctx)
        self._struct_change_bars = self._struct_change_bars + 1 if stale else 0

        baseline = atr(self.store.recent(rc.baseline_atr_period + 1), rc.baseline_atr_period)
        past_floor = self._bars_since_author >= rc.min_interval_bars
        if self._bars_since_author >= rc.max_interval_bars:
            self._reauthor_now(ctx, f"ceiling({rc.max_interval_bars}b)")
        elif past_floor and stale and self._struct_change_bars >= rc.confirm_bars:
            self._reauthor_now(ctx, f"{stale} x{self._struct_change_bars}b")
        elif past_floor and is_volatility_shock(ctx.atr, baseline, rc.shock_ratio):
            self._reauthor_now(ctx, "volatility_shock")

    def _playbook_stale(self, ctx: MarketContext) -> str | None:
        """Why the authored playbook no longer fits the live market, or None if it still does.

        - ``trend_flip``: the live trend turned opposite to the trend the playbook was authored
          under — its directional setups are now on the wrong side.
        - ``no_setup_for``: no authored setup is tagged for the live regime, so the brain has
          nothing to arm (benched) and a fresh playbook is needed for this market.

        A trend read of "flat" (off-trend) is not a flip; an untagged setup covers any regime
        (see ``_regime_covered``) so a missing tag never forces a re-author on its own."""
        a_trend = self._authored_trend
        if (a_trend in ("up", "down") and ctx.trend in ("up", "down")
                and ctx.trend != a_trend):
            return f"trend_flip({a_trend}->{ctx.trend})"
        if not self._regime_covered(ctx.regime):
            return f"no_setup_for({ctx.regime})"
        return None

    def _regime_covered(self, live_regime: str) -> bool:
        """Does any authored setup apply in ``live_regime``? An untagged setup (no clean regime
        tag) is treated as covering any regime, so a missing tag never benches the brain."""
        setups = self.agent.generated_strategies() or []
        return any(
            (s.get("regime") or "").strip().lower() in ("", live_regime)
            for s in setups
        )

    def _reauthor_now(self, ctx: MarketContext, why: str) -> None:
        print(f"[reauthor] {why}: bars_since_author={self._bars_since_author} "
              f"live={ctx.regime}/{ctx.trend} "
              f"authored={self._authored_regime}/{self._authored_trend}", flush=True)
        self._trigger_session_study(self.store.all(), force=True, outcome=f"reauthor:{why}")

    def _levels(self, bars: list[Bar]) -> list[Level]:
        lc = self.cfg.levels
        if not lc.enabled:
            return []
        return detect_levels(
            bars, lookback=lc.lookback, tick_size=self.cfg.instrument.tick_size,
            merge_ticks=lc.merge_ticks, min_touches=lc.min_touches,
            max_levels=lc.max_levels,
        )

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
        if fill_ts - float(p.get("ts", 0.0)) > effective_entry_freshness_s(self.cfg) + tf_s:
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
