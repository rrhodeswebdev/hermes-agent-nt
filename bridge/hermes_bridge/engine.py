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
from dataclasses import dataclass, field, replace
from itertools import count

from .agent_client import AgentClient, AgentRequest, MockAgentClient
from .config import BridgeConfig, effective_entry_freshness_s, timeframe_seconds
from .indicators import MarketContext, atr, build_context
from .journal import ClosedTrade, DeclineLog, JournalStore, TradeTracker
from .levels import detect_levels
from .market_calendar import closing_reason, within_close_cutoff
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
from .reauthor import ReauthorState, record_authored, step
from .risk import RiskGate
from .session import SessionState
from .stops import managed_stop_price, risk_scale_for_atr
from .store import BarStore

_CONTEXT_WINDOW = 200  # bars handed to indicator/context building


def _delta_confirms(
    action: Action,
    delta_ratio: float,
    floor: float,
    recent_signs: tuple[int, ...] | list[int] = (),
    sustain_bars: int = 0,
) -> bool:
    """Does order flow confirm an ENTRY's direction at ``floor``?

    Two independent ways to confirm (either suffices):
    - a SPIKE: the windowed ``delta_ratio`` clears the floor (long >= +floor, short <= -floor);
    - a SUSTAINED lean (only when ``sustain_bars`` > 0): ``delta_ratio`` has held the direction's
      sign for the last ``sustain_bars`` decision-bars — the persistent ETH grind a one-bar spike
      test misses, qualifying even when this bar's magnitude is below the floor.

    ``floor`` <= 0 always confirms (the gate is off). The sustained branch is a pure
    sign-persistence test (no magnitude), so it only ever RELAXES the gate — enable it with a
    calibrated floor, never alone."""
    if floor <= 0.0:
        return True
    want = 1 if action == Action.ENTER_LONG else -1
    spike_ok = delta_ratio >= floor if want > 0 else delta_ratio <= -floor
    if spike_ok:
        return True
    if sustain_bars > 0 and len(recent_signs) >= sustain_bars:
        return all(s == want for s in recent_signs[-sustain_bars:])
    return False


@dataclass(frozen=True)
class EngineResult:
    decision: Decision
    command: OrderCommand | None = None
    mode: str = ""
    risk_reasons: list[str] = field(default_factory=list)


@dataclass
class PendingCounterfactual:
    """An entry setup the brain ARMED but did not take — replayed forward to see whether
    declining it was right. ``limit_price`` is the entry we wanted; once a later bar touches
    it the replay is ``filled`` and tracked to its ATR bracket. Resolved outcomes
    (would_win / would_lose / ambiguous / never_filled / no_resolution) land in the
    DeclineLog — the over-blocking evidence a closed-trades journal can never carry."""

    kind: str
    side: Side
    limit_price: float
    stop_price: float
    target_price: float
    born_ts: float
    bars_left: int
    rationale: str
    regime: str
    filled: bool = False
    entry_price: float = 0.0
    fill_ts: float = 0.0
    # Gate attribution (item 2A): which gate suppressed the matching entry on the decline bar
    # ("min_confidence" | "transitional" | "delta_floor"; "" = this trigger did not match price
    # that bar, a purely speculative replay), plus the order flow / authored confidence at
    # decline. A would-win decline then becomes a precise "THIS gate cost a winner" signal that
    # reflection (and a re-score tool) can cluster by gate + session, instead of a bare miss.
    suppressed_by: str = ""
    delta_ratio: float = 0.0
    confidence: float = 0.0


@dataclass
class RegimeSmoother:
    """Temporal hysteresis on the mechanical (regime, trend) label. ``classify_regime`` is
    stateless and can flip bar-to-bar on a single mixed pivot; that thrash drives needless
    re-authoring and directional indecision. A NEW (regime, trend) read must persist for
    ``min_bars`` CONSECUTIVE bars before it replaces the committed label. ``min_bars`` <= 1
    is a no-op (adopt every read = the raw classifier)."""

    min_bars: int = 1
    regime: str = ""
    trend: str = ""
    _cand: tuple[str, str] | None = field(default=None, init=False)
    _streak: int = field(default=0, init=False)

    def update(self, regime: str, trend: str) -> tuple[str, str]:
        """Feed the raw per-bar read; return the (possibly held-over) committed label."""
        if not self.regime or (regime, trend) == (self.regime, self.trend):
            # first read, or the live read confirms the committed label
            self.regime, self.trend = regime, trend
            self._cand, self._streak = None, 0
        else:
            # a read that differs from the committed label — must persist min_bars bars
            if self._cand == (regime, trend):
                self._streak += 1
            else:
                self._cand, self._streak = (regime, trend), 1
            if self._streak >= max(1, self.min_bars):
                self.regime, self.trend = regime, trend
                self._cand, self._streak = None, 0
        return self.regime, self.trend


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
        declines: DeclineLog | None = None,
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
        # session.realized_pnl baseline stamped when the position leaves flat, so a trade
        # that EXITS across several partial fills journals the WHOLE-trade P&L at close (the
        # per-fill delta would otherwise drop every exit leg but the last). 0.0 while flat.
        self._trade_open_pnl: float = 0.0
        # Initial protective-stop distance (ticks) of the OPEN position, promoted from the
        # matching pending entry when the position actually FILLS (not at approval — a
        # dropped/stale order must not leave a stale 1R behind). The trade manager uses it
        # as 1R to decide when to pull the stop to breakeven / start trailing. None while flat.
        self._active_stop_ticks: int | None = None
        # High-water managed-stop price for the open trade — the trail RATCHETS through this so
        # it can only ever tighten (a transient lower/looser swing never loosens a live stop).
        # None until +1R engages the managed phase; reset to None on flat.
        self._managed_level: float | None = None
        self._ids = count(1)
        self.on_close = on_close
        self._prefilter = MockAgentClient(config) if config.agent.prefilter == "mock" else None
        self.last_context: MarketContext | None = None  # agent regime / S/R for the dashboard
        # Re-author state (agent mode): an immutable value (bar clocks + structural anchor)
        # threaded through reauthor.step each bar. The reducer decides WHEN to refresh the
        # authored playbook; the engine owns the guards + the act (see _maybe_reauthor).
        self.reauthor_state = ReauthorState()
        # Temporal hysteresis on the mechanical regime label — smooths build_context's read
        # before any consumer (decision, counterfactual tag, reauthor governor) sees it, so a
        # one-bar structural wiggle can't thrash re-authoring/bias. 1 = off. See RegimeSmoother.
        self.regime_smoother = RegimeSmoother(min_bars=config.strategy.regime_hysteresis_bars)
        # Last Claude-DECLINED prefilter candidate: {action, price, ts}. Near-identical
        # candidates are answered from this memo instead of burning another Claude call
        # (extended trends produce the same candidate bar after bar). See _duplicate_decline.
        self._declined: dict | None = None
        # Counterfactual replay of NOT-taken setups (gated by learning.counterfactuals_enabled).
        # _cf_seen dedups by (direction, band-bucket) so the plan cycle's per-bar re-arm of the
        # same entry zone is recorded once, not every bar. See _record_missed_triggers.
        self.declines = declines
        self._cf_pending: list[PendingCounterfactual] = []
        # Recent delta_ratio SIGNS (most recent last, bounded tail) for the sustained-delta gate
        # (strategy.delta_sustain_bars). Appended once per bar; only the last N are ever read.
        self._delta_signs: list[int] = []

    def _new_id(self) -> str:
        return f"{self.cfg.strategy_id}-{next(self._ids)}"

    # ---- bar handling -------------------------------------------------------
    def on_bar(self, bar: Bar) -> EngineResult:
        self.session.maybe_roll_day(bar.ts)
        self.store.append(bar)
        self.session.mark_bar(bar.ts)
        if self.session.position != 0:
            self.tracker.on_bar(bar)
        # Advance any not-taken setups against this bar before the new decision (a setup
        # recorded last bar first gets a touch/resolve chance here — never on its own bar).
        self._resolve_counterfactuals(bar)

        # If the daily goal/limit was hit on a prior fill and we are still in a
        # position, flatten immediately regardless of what the agent thinks.
        if self.session.halted and self.session.position != 0:
            cmd = self.flatten_command(self.session.halt_reason or "halted")
            rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
            return EngineResult(
                Decision(action=Action.FLATTEN, rationale=cmd.reason),
                rd.command, "halt_flatten", rd.reasons,
            )

        # Stand down for exchange holidays / early closes: flatten any open position ahead of
        # the close so it can't carry the holiday/weekend gap, and take no new entries for the
        # rest of that session. Deterministic + server-side (same authority as the halt flatten
        # above), never the brain. Display mirrors this via indicators.entry_window_state.
        if within_close_cutoff(bar.ts, self.cfg.risk.early_close_flat_lead_min):
            reason = closing_reason(bar.ts) or "early_close"
            if self.session.position != 0:
                cmd = self.flatten_command(reason)
                rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
                return EngineResult(
                    Decision(action=Action.FLATTEN, rationale=reason),
                    rd.command, "calendar_flatten", rd.reasons,
                )
            return EngineResult(
                Decision(action=Action.WAIT, rationale=reason), None, "calendar_closed")

        bars = self.store.recent(_CONTEXT_WINDOW)
        ctx = build_context(
            bars,
            atr_period=self.cfg.strategy.atr_period,
            swing_lookback=self.cfg.strategy.swing_lookback,
            level_bars=self.store.all(),  # multi-day reference levels need the full store
        )
        # Hysteresis: hold the committed regime/trend until a new read persists, so a one-bar
        # structural wiggle can't thrash re-authoring or flip directional bias (RegimeSmoother).
        sregime, strend = self.regime_smoother.update(ctx.regime, ctx.trend)
        if (sregime, strend) != (ctx.regime, ctx.trend):
            ctx = replace(ctx, regime=sregime, trend=strend)
        self.last_context = ctx  # expose current regime / S/R to the dashboard
        # Track the sign of the windowed delta for the sustained-delta gate (bounded tail).
        self._delta_signs.append(
            1 if ctx.delta_ratio > 0 else -1 if ctx.delta_ratio < 0 else 0)
        # Keep at least delta_sustain_bars (floor 64), else a wide sustain window can never
        # satisfy the len(recent_signs) >= sustain_bars guard in _delta_confirms.
        keep = max(64, self.cfg.strategy.delta_sustain_bars)
        del self._delta_signs[:-keep]
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

        # Deterministic winner-management (breakeven after +1R, then structure trail),
        # enforced HERE so it holds under both brains and the plan cycle — never delegated
        # to the LLM. It can only force a tighter EXIT, never open or hold against the brain.
        if mode == "manage_position":
            forced = self._managed_exit(ctx, bar)
            if forced is not None:
                decision = forced

        # Gate entries (exits always honored). Capture WHICH gate first turns an ENTRY into a
        # WAIT so the counterfactual record can later attribute a would-win to the exact gate
        # that blocked it (item 2A). min_confidence first, then the two delta gates.
        sp = self.cfg.strategy
        suppressed_by = ""
        if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            if decision.confidence < sp.min_confidence:
                decision = Decision(action=Action.WAIT,
                                    rationale=f"low_confidence:{decision.confidence}")
                suppressed_by = "min_confidence"
        # Stand down in an unclear/transitional regime (config-gated). With wait_in_transitional
        # ON this is a blanket WAIT; with it OFF and a transitional_delta_floor set, a
        # transitional ENTRY is allowed only when order flow confirms at that (session-scaled)
        # floor — so a brain that authored a setup can't fire it into chop, but a delta-confirmed
        # breakout still goes. Exits/management pass through.
        before = decision.action
        decision = self._suppress_transitional(
            decision, ctx.regime, sp.wait_in_transitional,
            ctx.delta_ratio, sp.transitional_delta_floor,
            session=ctx.session, eth_scale=sp.eth_delta_scale,
            recent_signs=self._delta_signs, sustain_bars=sp.delta_sustain_bars)
        if not suppressed_by and before in (Action.ENTER_LONG, Action.ENTER_SHORT) and (
                decision.action == Action.WAIT):
            suppressed_by = "transitional"
        # Require order-flow confirmation: the armed plan trigger fires on a price band alone
        # (plan.evaluate_plan is price-only), so the (session-scaled, optionally sustained) delta
        # floor a setup specifies is enforced HERE — under both brains and the plan cycle.
        # Exits/management pass through.
        before = decision.action
        decision = self._suppress_low_delta(
            decision, ctx.delta_ratio, sp.delta_floor,
            session=ctx.session, eth_scale=sp.eth_delta_scale,
            recent_signs=self._delta_signs, sustain_bars=sp.delta_sustain_bars)
        if not suppressed_by and before in (Action.ENTER_LONG, Action.ENTER_SHORT) and (
                decision.action == Action.WAIT):
            suppressed_by = "delta_floor"

        if decision.action == Action.WAIT:
            self._remember_decline(candidate, bar)
            result = EngineResult(decision, None, mode)
        else:
            self._declined = None  # an actionable decision invalidates the memo
            cmd = self._to_command(decision)
            # Shrink the per-trade dollar budget in a volatility shock (entries only;
            # risk-reducing actions are never scaled).
            scale = (
                self._risk_scale(ctx)
                if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT)
                else 1.0
            )
            rd = self.risk.evaluate(
                cmd, self.session, last_price=bar.close, now_ts=bar.ts, risk_scale=scale,
                confidence=decision.confidence, atr=ctx.atr,
            )
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
                    # 1R for the trade manager — promoted to _active_stop_ticks only if/when
                    # THIS order fills (see on_fill); a dropped order leaves nothing stale.
                    "stop_ticks": self._command_stop_ticks(rd.command, bar.close),
                    # Absolute (stop, target) for the exit-replay (learning.exit_replays_enabled):
                    # the trade's original bracket, scored against later bars when it closes.
                    "brackets": self._command_brackets(rd.command, bar.close),
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
            self._record_missed_triggers(armed, bar, ctx, result, suppressed_by)
            self._schedule_followup(bar, ctx, bars, account, result)
        return result

    @staticmethod
    def _suppress_transitional(
        decision: Decision, regime: str, enabled: bool,
        delta_ratio: float = 0.0, transitional_delta_floor: float = 0.0,
        *, session: str = "", eth_scale: float = 1.0,
        recent_signs: tuple[int, ...] | list[int] = (), sustain_bars: int = 0,
    ) -> Decision:
        """Gate ENTRIES in a 'transitional' regime (config-driven, three modes). Exits and
        position management are never gated; trending/ranging pass through untouched.

        - enabled (wait_in_transitional) True  -> blanket WAIT (strictest belt; the legacy
          behavior, unchanged).
        - enabled False, transitional_delta_floor > 0 -> allow only if order flow confirms at
          this STRICTER, session-scaled floor (a spike, or a sustained same-sign lean — see
          _delta_confirms), else WAIT. Transitional structure (mixed/breaking, or too few pivots
          yet) needs stronger proof a breakout is real. Stacks above the global delta_floor
          (_suppress_low_delta): in transitional the effective bar is the stricter of the two.
        - enabled False, transitional_delta_floor 0 -> no transitional-specific gate.
        """
        if (regime != "transitional"
                or decision.action not in (Action.ENTER_LONG, Action.ENTER_SHORT)):
            return decision
        if enabled:
            return Decision(
                action=Action.WAIT,
                rationale=f"transitional_regime_wait (suppressed {decision.action.value})")
        if transitional_delta_floor > 0.0:
            floor = transitional_delta_floor * (eth_scale if session == "ETH" else 1.0)
            if not _delta_confirms(
                    decision.action, delta_ratio, floor, recent_signs, sustain_bars):
                sign = "+" if decision.action == Action.ENTER_LONG else "-"
                return Decision(
                    action=Action.WAIT,
                    rationale=(
                        f"transitional_delta_below_floor (delta={delta_ratio:+.3f} vs "
                        f"{sign}{floor:g}; suppressed {decision.action.value})"))
        return decision

    @staticmethod
    def _suppress_low_delta(
        decision: Decision, delta_ratio: float, floor: float,
        *, session: str = "", eth_scale: float = 1.0,
        recent_signs: tuple[int, ...] | list[int] = (), sustain_bars: int = 0,
    ) -> Decision:
        """Convert an ENTRY to WAIT when order-flow delta does not confirm the direction
        (config-gated). The armed plan trigger fires on a price band alone, so the delta floor
        a setup names is otherwise never enforced mechanically. Confirmation is a spike
        (delta_ratio >= +floor long / <= -floor short) OR, when sustain_bars > 0, a sustained
        same-sign lean (see _delta_confirms). The floor is session-scaled: in ETH it becomes
        floor * eth_scale (a lighter, balanced tape rarely spikes to an RTH-sized floor). floor
        <= 0 disables the gate (the neutral default; also avoids suppressing replay/backtests
        with no order-flow data). Exits and position management are never gated."""
        if floor <= 0.0 or decision.action not in (Action.ENTER_LONG, Action.ENTER_SHORT):
            return decision
        eff = floor * (eth_scale if session == "ETH" else 1.0)
        if _delta_confirms(decision.action, delta_ratio, eff, recent_signs, sustain_bars):
            return decision
        sign = "+" if decision.action == Action.ENTER_LONG else "-"
        return Decision(
            action=Action.WAIT,
            rationale=f"delta_below_floor (delta={delta_ratio:+.3f} vs {sign}{eff:g}; "
                      f"suppressed {decision.action.value})")

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
            level_bars=bars,  # the full study history, for multi-day reference levels
        )
        account = self.session.account_state(mark_price=bars[-1].close)
        mode: Mode = "manage_position" if self.session.position != 0 else "seek_entry"
        # Anchor the structural staleness check to what this study authors from, so the next
        # re-author fires when the live market drifts off THIS read (not the previous one).
        self.reauthor_state = record_authored(ctx)
        self.planner.schedule_session_analysis(bars, PlanRequest(
            mode=mode, context=ctx, recent_bars=recent, account=account,
            bar_ts=bars[-1].ts, assumed_position=self.session.position,
            levels=self._levels(recent), outcome=outcome,
        ), force=force)

    def _maybe_reauthor(self, ctx: MarketContext) -> None:
        """Structure-driven re-author (agent mode): refresh the playbook when the live market
        no longer matches the one the brain authored. The engine owns the guards and the act;
        the ``ReauthorGovernor`` owns the decision + why (see reauthor.py). The old playbook
        keeps trading until the new one lands (no WAIT gap)."""
        rc = self.cfg.strategies.reauthor
        if (not rc.enabled or self.planner is None
                or self.agent.strategy_source() != "agent"
                or self.planner.is_analyzing_session()):          # one already in flight
            return
        baseline = atr(self.store.recent(rc.baseline_atr_period + 1), rc.baseline_atr_period)
        self.reauthor_state, reason = step(
            self.reauthor_state, ctx, cfg=rc,
            generated_strategy=self.agent.generated_strategy(),
            generated_strategies=self.agent.generated_strategies(),
            baseline_atr=baseline,
        )
        if reason is not None:
            self._reauthor_now(ctx, reason)

    def _reauthor_now(self, ctx: MarketContext, why: str) -> None:
        s = self.reauthor_state
        print(f"[reauthor] {why}: bars_since_author={s.bars_since_author} "
              f"live={ctx.regime}/{ctx.trend} "
              f"authored={s.authored_regime}/{s.authored_trend}", flush=True)
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

    # ---- counterfactual replay of not-taken setups --------------------------
    def _resolve_counterfactuals(self, bar: Bar) -> None:
        """Advance each pending not-taken setup against the just-closed bar; resolved
        outcomes append to the DeclineLog. No-op when the feature is off or nothing pends."""
        if self.declines is None or not self._cf_pending:
            return
        still: list[PendingCounterfactual] = []
        for p in self._cf_pending:
            outcome = self._cf_step(p, bar)
            if outcome is None:
                still.append(p)
                continue
            self.declines.append({
                "kind": p.kind, "outcome": outcome, "side": p.side.value,
                "limit_price": round(p.limit_price, 4),
                "stop_price": round(p.stop_price, 4),
                "target_price": round(p.target_price, 4),
                "regime": p.regime, "rationale": p.rationale,
                # Gate attribution (item 2A): which gate blocked it + the order flow / confidence
                # at decline, so a would-win can be clustered by gate + session for reflection.
                "suppressed_by": p.suppressed_by,
                "delta_ratio": round(p.delta_ratio, 4),
                "confidence": round(p.confidence, 3),
                # Full timeline so the outcome can be re-verified later without guessing
                # the anchor: born_ts = the bar it was declined on (replay starts here),
                # fill_ts = when the limit was touched (None if never filled), resolved_ts
                # = the bar that decided the outcome.
                "born_ts": p.born_ts,
                "fill_ts": p.fill_ts or None,
                "resolved_ts": bar.ts,
            })
        self._cf_pending = still

    @staticmethod
    def _cf_step(p: PendingCounterfactual, bar: Bar) -> str | None:
        """One replay step; returns an outcome when resolved, else None (still pending).
        Never credits a win/loss on the fill bar — intra-bar order is unknown, so a bar that
        spans BOTH brackets is 'ambiguous', never a fabricated loss."""
        if not p.filled:
            touched = (bar.low <= p.limit_price if p.side == Side.LONG
                       else bar.high >= p.limit_price)
            if touched:
                p.filled = True
                p.entry_price = p.limit_price
                p.fill_ts = bar.ts
                return None  # resolution starts on the bar AFTER the fill
            p.bars_left -= 1
            return "never_filled" if p.bars_left <= 0 else None
        if p.side == Side.LONG:
            target_hit, stop_hit = bar.high >= p.target_price, bar.low <= p.stop_price
        else:
            target_hit, stop_hit = bar.low <= p.target_price, bar.high >= p.stop_price
        if target_hit and stop_hit:
            return "ambiguous"
        if target_hit:
            return "would_win"
        if stop_hit:
            return "would_lose"
        p.bars_left -= 1
        return "no_resolution" if p.bars_left <= 0 else None

    def _record_missed_triggers(self, plan: TradePlan | None, bar: Bar,
                                ctx: MarketContext, result: EngineResult,
                                suppressed_by: str = "") -> None:
        """Record (deduped) the entry triggers the brain armed but did NOT fire this close,
        so the replay can later score whether declining them was right. Gated off by default
        (learning.counterfactuals_enabled). The trunk re-arms a plan every bar, so the dedup
        is load-bearing: without it the same pullback band would log on every bar."""
        if (self.declines is None or not self.cfg.learning.counterfactuals_enabled
                or plan is None or plan.mode != "seek_entry"
                or plan.based_on_bar_ts >= bar.ts or not plan.triggers):
            return
        if result.command is not None and result.command.action in (
            Action.ENTER_LONG, Action.ENTER_SHORT
        ):
            return  # the plan fired — that's a real (journaled) trade, not a miss
        atr_value = ctx.atr or 0.0
        tick = self.cfg.instrument.tick_size or 0.25
        for t in plan.triggers:
            side = Side.LONG if t.direction == "long" else Side.SHORT
            # Entry = the band edge price first reaches on its way into the zone.
            limit = ((t.max_close if t.max_close is not None else t.min_close)
                     if side == Side.LONG
                     else (t.min_close if t.min_close is not None else t.max_close))
            if limit is None:
                continue
            stop_ticks, target_ticks = t.stop_ticks, t.target_ticks
            if stop_ticks is None and atr_value > 0:
                stop_ticks = max(1, round(self.cfg.strategy.atr_stop_mult * atr_value / tick))
            if target_ticks is None and atr_value > 0:
                target_ticks = max(1, round(self.cfg.strategy.atr_target_mult * atr_value / tick))
            if not stop_ticks or not target_ticks:
                continue
            if side == Side.LONG:
                stop_price, target_price = limit - stop_ticks * tick, limit + target_ticks * tick
            else:
                stop_price, target_price = limit + stop_ticks * tick, limit - target_ticks * tick
            # Dedup by proximity to a same-side pending: the plan cycle re-arms the same band
            # every bar, so without this one missed pullback would log on every bar. tol uses
            # the live ATR but the compare is on raw price, so it stays stable as ATR drifts.
            tol = self.cfg.learning.counterfactual_dedup_atr * atr_value
            if any(p.kind == "missed_trigger" and p.side == side
                   and abs(p.limit_price - limit) <= tol for p in self._cf_pending):
                continue
            self._cf_pending.append(PendingCounterfactual(
                kind="missed_trigger", side=side, limit_price=limit,
                stop_price=stop_price, target_price=target_price, born_ts=bar.ts,
                bars_left=self.cfg.learning.counterfactual_horizon_bars,
                rationale=t.rationale or plan.rationale, regime=ctx.regime,
                # Attribute the blocking gate only to the trigger that actually matched price
                # this bar (the one evaluate_plan turned into the suppressed ENTRY); the others
                # are speculative replays that simply never triggered.
                suppressed_by=suppressed_by if t.matches(bar.close) else "",
                delta_ratio=ctx.delta_ratio, confidence=t.confidence,
            ))

    def _record_exit_replay(self, trade: ClosedTrade) -> None:
        """Score a NON-target exit by replaying it forward on the trade's ORIGINAL bracket:
        would_win = the exit left money (price reached the target — a shakeout), would_lose =
        the exit dodged the stop. Pre-marked filled so _cf_step tracks target/stop straight
        from the next bar. Gated by learning.exit_replays_enabled; needs a real bracket."""
        if (self.declines is None or not self.cfg.learning.exit_replays_enabled
                or not trade.target_price or not trade.stop_price):
            return
        side = Side.LONG if trade.side == "LONG" else Side.SHORT
        # Already at/through target on exit = it won, not an early exit — nothing to replay.
        reached = (trade.exit_price >= trade.target_price if side == Side.LONG
                   else trade.exit_price <= trade.target_price)
        if reached:
            return
        self._cf_pending.append(PendingCounterfactual(
            kind="early_exit", side=side, limit_price=trade.exit_price,
            stop_price=trade.stop_price, target_price=trade.target_price, born_ts=trade.exit_ts,
            bars_left=self.cfg.learning.counterfactual_horizon_bars,
            rationale=trade.rationale, regime=str(trade.entry_context.get("regime", "")),
            filled=True, entry_price=trade.exit_price, fill_ts=trade.exit_ts,
        ))

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
            # Arm the trade manager's 1R from THIS fill's order; an unattributed fill (no
            # matching pending) leaves it None so breakeven/trail simply won't engage on a
            # trade whose real stop we don't know — the resting bracket still protects it.
            self._active_stop_ticks = p.get("stop_ticks") if p is not None else None
            self._managed_level = None
            self._trade_open_pnl = before_pnl  # baseline for the whole-trade P&L at close
            ctx = p["context"] if p is not None else self.last_context
            if ctx is not None:  # no context at all (fill before any bar): nothing to journal
                sp, tp = p.get("brackets", (0.0, 0.0)) if p is not None else (0.0, 0.0)
                self.tracker.on_entry(
                    ts=fill.ts, side=side, qty=abs(after_pos), price=fill.price,
                    context=ctx,
                    rationale=p["rationale"] if p is not None
                    else "unattributed_fill (no matching pending entry)",
                    confidence=p.get("confidence", 0.0) if p is not None else 0.0,
                    stop_price=sp, target_price=tp,
                )
            self._pending_entry = None  # consumed or invalidated either way
        elif (before_pos != 0 and abs(after_pos) > abs(before_pos)
                and (after_pos > 0) == (before_pos > 0)):
            # Scaled into the SAME-side open position on a later fill (a partial entry
            # completing, or pyramiding). Without this the trade journals at only its first
            # leg's size — the live 2-lots-booked-as-1-lot under-count. Track peak size +
            # the running weighted-average entry so it closes as one full-size trade.
            self.tracker.note_scale(qty=abs(after_pos), avg_price=self.session.avg_price)
        elif before_pos != 0 and after_pos == 0:
            # Flat: the trade manager's 1R and trailed high-water no longer apply.
            self._active_stop_ticks = None
            self._managed_level = None
            # WHOLE-trade P&L since it left flat — not just this last exit leg's delta. A
            # multi-fill exit realizes across several on_fill calls, so the per-call
            # `realized_pnl - before_pnl` would drop every leg but the last; the open
            # baseline (_trade_open_pnl) captures the full round trip.
            trade = self.tracker.on_exit(
                ts=fill.ts, price=fill.price,
                realized_pnl=self.session.realized_pnl - self._trade_open_pnl,
            )
            if trade is not None:
                if self.journal is not None:
                    self.journal.append(trade)
                self._record_exit_replay(trade)
                if self.on_close is not None:
                    self.on_close(trade)
        # else: a partial REDUCE toward flat (position still open) — keep tracking; the
        # close branch journals the whole trade when it finally returns to flat. (The
        # strategy flattens before reversing, so a direct long<->short flip never occurs.)

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

    def _command_brackets(self, cmd: OrderCommand, entry_ref: float) -> tuple[float, float]:
        """The order's ABSOLUTE (stop_price, target_price). Prefer explicit prices; else
        derive from ticks around ``entry_ref``. (0.0, 0.0) when neither is known."""
        tick = self.cfg.instrument.tick_size or 0.25
        sign = 1.0 if cmd.action == Action.ENTER_LONG else -1.0  # long: stop below / target above
        sp = cmd.stop_price
        if sp is None and cmd.stop_ticks:
            sp = entry_ref - sign * cmd.stop_ticks * tick
        tp = cmd.target_price
        if tp is None and cmd.target_ticks:
            tp = entry_ref + sign * cmd.target_ticks * tick
        return float(sp or 0.0), float(tp or 0.0)

    def _command_stop_ticks(self, cmd: OrderCommand, entry_price: float) -> int | None:
        """The protective-stop distance in ticks of an approved entry (1R for the trade
        manager). Prefers the explicit stop_ticks; derives it from a price stop otherwise."""
        if cmd.stop_ticks is not None:
            return cmd.stop_ticks
        if cmd.stop_price is not None:
            tick = self.cfg.instrument.tick_size or 0.25
            return max(1, round(abs(entry_price - cmd.stop_price) / tick))
        return None

    def _risk_scale(self, ctx: MarketContext) -> float:
        """Per-trade risk-budget multiplier for the live volatility regime (shrinks size in
        a shock). Reuses the re-author baseline-ATR window + shock_ratio for one shock read."""
        rc = self.cfg.strategies.reauthor
        baseline = atr(self.store.recent(rc.baseline_atr_period + 1), rc.baseline_atr_period)
        return risk_scale_for_atr(ctx.atr, baseline, self.cfg)

    def _managed_exit(self, ctx: MarketContext, bar: Bar) -> Decision | None:
        """A forced EXIT when the just-closed bar breaches the position's MANAGED stop
        (breakeven once +1R favorable, then trailing behind structure). None means leave the
        decision to the brain/plan — pre-+1R, the feature is off, or the stop isn't breached —
        so the wide initial bracket and the brain's structural exit are unchanged until then."""
        pos = self.session.position
        if pos == 0:
            return None
        side = Side.LONG if pos > 0 else Side.SHORT
        exc = self.tracker.open_excursion()
        mfe = exc[1] if exc is not None else 0.0
        level = managed_stop_price(
            side=side, entry=self.session.avg_price,
            initial_stop_ticks=self._active_stop_ticks, mfe=mfe,
            swing_low=ctx.swing_low, swing_high=ctx.swing_high, cfg=self.cfg,
        )
        if level is None:
            return None
        # Ratchet: the managed stop can only ever TIGHTEN toward price (up for a long, down
        # for a short), so a transient looser swing never loosens a live stop.
        if self._managed_level is None:
            self._managed_level = level
        elif side == Side.LONG:
            self._managed_level = max(self._managed_level, level)
        else:
            self._managed_level = min(self._managed_level, level)
        level = self._managed_level
        close = bar.close
        breached = (
            (side == Side.LONG and close <= level)
            or (side == Side.SHORT and close >= level)
        )
        if not breached:
            return None
        return Decision(
            action=Action.EXIT, confidence=0.95, qty=abs(pos),
            rationale=f"managed_stop({side.value.lower()} @{level:g}): "
                      f"breakeven/trail hit on close {close:g}",
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
