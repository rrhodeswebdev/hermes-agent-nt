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
from .shadow_be import shadow_breakeven_outcome
from .stops import (
    managed_stop_price,
    plan_exit_stop_price,
    risk_scale_for_atr,
    tightest_stop,
)
from .store import BarStore

_CONTEXT_WINDOW = 200  # bars handed to indicator/context building


def setup_regime_mismatch(
    setup: str | None,
    roster: list[dict] | None,
    live_regime: str | None,
) -> str | None:
    """The setup's DECLARED regime when it contradicts ``live_regime``, else None.

    Every authored setup ships the regime it is for ({name, regime}); the armed trigger is
    bound to one of them by name against that same roster, so this compares two FIELDS and
    never reads a rationale. That is the whole point: the hard rule was already written down
    and in the prompt, and the narrative talked past it anyway (see config.enforce_setup_regime).

    Fails OPEN — an unknown setup, an untagged roster entry, or a missing live regime all
    return None. A roster or plumbing gap must degrade to the previous behavior, never to a
    silent trading halt."""
    if not setup or not roster or not live_regime:
        return None
    want = setup.strip().lower()
    for s in roster:
        name = (s.get("name") or "").strip().lower()
        if name and name == want:
            declared = (s.get("regime") or "").strip().lower()
            if not declared:
                return None                      # untagged setup: nothing to contradict
            return declared if declared != live_regime.strip().lower() else None
    return None                                  # setup not on the roster: cannot judge


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
    # Risk-reducing commands queued ALONGSIDE the decision (today: AMEND_STOP, which moves
    # the working protective stop without opening or closing anything). Kept separate so the
    # single-command contract — and everything that reads `command` — is unchanged.
    extra_commands: list[OrderCommand] = field(default_factory=list)


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
    # The trailing windowed-delta SIGNS the sustained-delta gate reads (_delta_confirms) and the
    # session (ETH scales the floor), snapshotted at the decline bar. Stamped onto the record so a
    # future rescore of the sustained branch reads the EXACT gate inputs instead of reconstructing
    # delta from bars.db (whose history-backfill bars carry no bid/ask).
    delta_signs: tuple[int, ...] = ()
    session: str = ""
    # Coverage-shape tag copied from the trigger (Package B): "" for untagged triggers,
    # "sign_persist" for grind-trend arms. Written to the decline record as "shape"
    # (omitted when empty) so the suppressed cohort is measurable in isolation.
    shape: str = ""


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
        decision_tf: Callable[[], str] | None = None,
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
        # Set True when a position goes flat (on_fill), consumed by _maybe_reauthor → a
        # post-trade re-author so the next setup is authored at current levels.
        self._post_trade_refresh = False
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
        # The CURRENT decision timeframe (the resampler varies it by session). Defaults to the
        # static config value, so the non-resampled path is unchanged.
        self._decision_tf_getter = decision_tf or (lambda: self.cfg.instrument.timeframe)

    def _new_id(self) -> str:
        return f"{self.cfg.strategy_id}-{next(self._ids)}"

    def _decision_tf(self) -> str:
        """The decision timeframe in force right now (resampler-driven, else the config value)."""
        return self._decision_tf_getter()

    def _account_for_brain(self, mark_price: float) -> AccountState:
        """The account snapshot the brain sees, enriched while a position is open with the open
        trade's excursion in points: peak favorable (mfe), peak adverse (mae), and how much of
        the peak has been handed back (giveback = mfe - current favorable excursion). This is what
        lets the between-bars analysis SEE a winner round-tripping — a fixed bracket + breakeven
        trail can't express it. Plain account when flat (the fields stay None)."""
        account = self.session.account_state(mark_price=mark_price)
        exc = self.tracker.open_excursion()
        if exc is None or self.session.position == 0:
            return account
        mae_pts, mfe_pts = exc
        avg = self.session.avg_price
        cur_fav = (mark_price - avg) if self.session.position > 0 else (avg - mark_price)
        return account.model_copy(update={
            "mfe_points": round(mfe_pts, 4),
            "mae_points": round(mae_pts, 4),
            "giveback_points": round(max(0.0, mfe_pts - cur_fav), 4),
        })

    # ---- bar handling -------------------------------------------------------
    def on_bar(self, bar: Bar) -> EngineResult:
        self.session.maybe_roll_day(bar.ts)
        self.store.append(bar)
        self.session.mark_bar(bar.ts)
        self._expire_pending_entry(bar.ts)
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
        account = self._account_for_brain(bar.close)
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
        # Setup/regime coherence: the armed setup declares the regime it is FOR; refuse to
        # fire it into a regime its own roster entry contradicts. Fields only, never the
        # rationale — the rule was already in the prompt and got narrated past six times.
        if sp.enforce_setup_regime and decision.action in (
                Action.ENTER_LONG, Action.ENTER_SHORT):
            plan = self.planner.current_plan() if self.planner is not None else None
            declared = setup_regime_mismatch(
                getattr(plan, "active_strategy", None),
                self.agent.generated_strategies(),
                ctx.regime,
            )
            if declared is not None:
                decision = Decision(action=Action.WAIT, rationale=(
                    f"setup_regime_mismatch:{declared}!={ctx.regime}"))
                if not suppressed_by:
                    suppressed_by = "setup_regime"

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
                # Exposure is in flight from the moment the order is approved, not from
                # the fill — this is what stops a second entry slipping through the gap.
                self.session.pending_entry_qty = rd.command.qty
                self._pending_entry = {
                    "cmd_id": rd.command.id,
                    "ts": bar.ts,
                    "side": Side.LONG if decision.action == Action.ENTER_LONG else Side.SHORT,
                    "context": ctx,
                    "rationale": decision.rationale,
                    "confidence": decision.confidence,
                    # The approved order itself, so the fill can re-anchor its bracket/1R to
                    # the REAL fill price (see on_fill). The bar-close values below stay as a
                    # fallback for a memo that carries no command (hand-built / legacy).
                    "command": rd.command,
                    # 1R for the trade manager — promoted to _active_stop_ticks only if/when
                    # THIS order fills (see on_fill); a dropped order leaves nothing stale.
                    "stop_ticks": self._command_stop_ticks(rd.command, bar.close),
                    # Absolute (stop, target) for the exit-replay (learning.exit_replays_enabled):
                    # the trade's original bracket, scored against later bars when it closes.
                    "brackets": self._command_brackets(rd.command, bar.close),
                    # What the gate DID to this entry (sizing ladder, confidence clamp, ...).
                    # Approved-order reasons never reach the decline log, so this memo is the
                    # only path by which the learning loop can see them. Journaled on fill.
                    "risk_reasons": list(rd.reasons or []),
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
        # Rest the armed protective level in NinjaTrader so it protects BETWEEN closes too.
        # Skipped when we're already leaving: an exit makes the book flat, so there is
        # nothing left to protect.
        if result.command is None or result.command.action not in (
            Action.EXIT, Action.FLATTEN
        ):
            amend = self._stop_amendment(ctx, bar, armed)
            if amend is not None:
                result = replace(result, extra_commands=[*result.extra_commands, amend])
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
        account = self._account_for_brain(bars[-1].close)
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
        just_closed = self._post_trade_refresh
        self._post_trade_refresh = False  # consume once past the guards (else it persists)
        self.reauthor_state, reason = step(
            self.reauthor_state, ctx, cfg=rc,
            generated_strategy=self.agent.generated_strategy(),
            generated_strategies=self.agent.generated_strategies(),
            baseline_atr=baseline,
            just_closed=just_closed,
        )
        if reason is not None:
            self._reauthor_now(ctx, reason)

    def _reauthor_now(self, ctx: MarketContext, why: str) -> None:
        s = self.reauthor_state
        print(f"[reauthor] {why}: bars_since_author={s.bars_since_author} "
              f"live={ctx.regime}/{ctx.trend} "
              f"authored={s.authored_regime}/{s.authored_trend}", flush=True)
        self._trigger_session_study(self.store.all(), force=True, outcome=f"reauthor:{why}")

    def reauthor(self, *, outcome: str) -> None:
        """Force a fresh pre-session study + playbook from the current store (agent mode).
        force=True keeps the old playbook trading until the new one lands (no WAIT gap).
        Used on a resampler session/TF switch."""
        self._trigger_session_study(self.store.all(), force=True, outcome=outcome)

    def _levels(self, bars: list[Bar]) -> list[Level]:
        lc = self.cfg.levels
        if not lc.enabled:
            return []
        return detect_levels(
            bars, lookback=lc.lookback, tick_size=self.cfg.instrument.tick_size,
            merge_ticks=lc.merge_ticks, min_touches=lc.min_touches,
            max_levels=lc.max_levels,
        )

    def _expire_pending_entry(self, now_ts: float) -> None:
        """Release the in-flight entry guard once no fill could still belong to that order.

        The guard fails CLOSED (it blocks new entries), so it must not be able to wedge the
        engine if an approved order neither fills nor is explicitly dropped. Uses the same
        freshness window as ``_matching_pending``, so the block lasts exactly as long as a
        fill could legitimately be attributed to it — one or two bars, which is the race
        window being closed, not longer."""
        p = self._pending_entry
        if p is None:
            return
        tf_s = timeframe_seconds(self._decision_tf())
        if now_ts - float(p.get("ts", 0.0)) > effective_entry_freshness_s(self.cfg) + tf_s:
            self._pending_entry = None
            self.session.pending_entry_qty = 0

    def entry_dropped(self, cmd_id: str) -> None:
        """The server dropped this queued entry (stale): disarm the journal memo so the
        next fill — from any source — is not attributed to its context/rationale."""
        p = self._pending_entry
        if p is not None and p.get("cmd_id") == cmd_id:
            self._pending_entry = None
            self.session.pending_entry_qty = 0  # never queued ⇒ no exposure in flight

    def _matching_pending(self, side: Side, fill_ts: float) -> dict | None:
        """The armed entry memo, only if it plausibly produced this fill: same side and
        recent (decision budget + one bar). Anything else means the fill came from another
        source (manual, /agent/command, a dropped command that filled anyway) — journaling
        it under the memo's rationale would teach the reflector from a mislabeled trade."""
        p = self._pending_entry
        if p is None or p.get("side") != side:
            return None
        tf_s = timeframe_seconds(self._decision_tf())
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
        tf_s = timeframe_seconds(self._decision_tf()) or 120
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
                # Exact sustained-delta-gate inputs at the decline bar (see PendingCounterfactual):
                # the trailing windowed-delta signs + session, so a rescore needs no reconstruction.
                "delta_signs": list(p.delta_signs),
                "session": p.session,
                # Coverage-shape cohort tag (Package B) — present only when tagged.
                **({"shape": p.shape} if p.shape else {}),
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
            # A shadowed (over-cap) trigger goes in its OWN bucket so the "did the $125 cap cost
            # a winner?" tally stays separate from the gate-skip scoreboard (delta/confidence).
            kind = "over_cap_trigger" if not t.feasible else "missed_trigger"
            # Dedup by proximity to a same-KIND pending: the plan cycle re-arms the same band
            # every bar, so without this one missed pullback would log on every bar. tol uses
            # the live ATR but the compare is on raw price, so it stays stable as ATR drifts.
            tol = self.cfg.learning.counterfactual_dedup_atr * atr_value
            if any(p.kind == kind and p.side == side
                   and abs(p.limit_price - limit) <= tol for p in self._cf_pending):
                continue
            self._cf_pending.append(PendingCounterfactual(
                kind=kind, side=side, limit_price=limit,
                stop_price=stop_price, target_price=target_price, born_ts=bar.ts,
                bars_left=self.cfg.learning.counterfactual_horizon_bars,
                rationale=t.rationale or plan.rationale, regime=ctx.regime,
                # A shadowed trigger is blocked by the RISK CAP, not a decision gate — attribute it
                # as such. Otherwise attribute the blocking gate only to the trigger that actually
                # matched price this bar (the suppressed ENTRY); the others are speculative replays.
                suppressed_by=("risk_cap" if not t.feasible
                               else suppressed_by if t.matches(bar.close) else ""),
                delta_ratio=ctx.delta_ratio, confidence=t.confidence,
                # Snapshot the gate's sustained-delta inputs at this bar (last 16 signs covers any
                # plausible delta_sustain_bars with headroom) + the session for the ETH floor scale.
                delta_signs=tuple(self._delta_signs[-16:]), session=ctx.session,
                # Coverage-shape tag copied straight from the trigger (Task 1's confirm_mode);
                # "" for untagged triggers so the decline record omits the key entirely.
                shape=(t.confirm_mode or ""),
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

    def _record_breakeven_shadow(self, trade: ClosedTrade) -> None:
        """Shadow-only breakeven tuning (evidence, NEVER an order): score a tighter,
        transitional-gated breakeven arming against the live +1R manager and log the
        counterfactual to the decline bucket. Gates a future regime-aware breakeven_r; the
        live manager (managed_stop_price) is untouched. Guarded — a shadow must never break
        the bar loop."""
        if self.declines is None or self.cfg.strategy.shadow_breakeven_r_transitional <= 0:
            return
        try:
            bars = [b for b in self.store.all() if trade.entry_ts <= b.ts <= trade.exit_ts]
            rec = shadow_breakeven_outcome(trade, bars, self.cfg)
            if rec is not None:
                self.declines.append(rec)
        except Exception as e:  # evidence-only; a shadow can't be allowed to break the loop
            print(f"[shadow_be] eval failed: {e}", flush=True)

    # ---- fill handling ------------------------------------------------------
    def on_fill(self, fill: Fill) -> OrderCommand | None:
        """Apply a fill, journal a completed trade on close, and flatten if the daily
        goal/limit tripped while still in a position."""
        before_pos = self.session.position
        before_pnl = self.session.realized_pnl
        # The order is no longer in flight — `position` now carries the exposure, so the
        # gate's flat-only check takes over from the in-flight guard.
        self.session.pending_entry_qty = 0
        self.session.apply_fill(fill)
        after_pos = self.session.position

        if before_pos == 0 and after_pos != 0:
            side = Side.LONG if after_pos > 0 else Side.SHORT
            p = self._matching_pending(side, fill.ts)
            # Re-anchor the bracket and 1R to the ACTUAL FILL. At approval time the fill price
            # does not exist yet, so the memo derives them around the bar close — but a TICK
            # bracket reaches NinjaTrader as CalculationMode.Ticks, which IT anchors to the real
            # entry fill. Journaling the close-anchored levels therefore recorded a bracket a
            # fill-gap away from the one actually resting (live 2026-07-28: both trades logged
            # stop/target exactly 1.00 off NT8's), corrupting the levels the learning loop reads.
            # An explicit-price bracket goes out as CalculationMode.Price and these helpers
            # return it verbatim, so re-anchoring is a no-op there.
            cmd = p.get("command") if p is not None else None
            # Arm the trade manager's 1R from THIS fill's order; an unattributed fill (no
            # matching pending) leaves it None so breakeven/trail simply won't engage on a
            # trade whose real stop we don't know — the resting bracket still protects it.
            if cmd is not None:
                self._active_stop_ticks = self._command_stop_ticks(cmd, fill.price)
            else:
                self._active_stop_ticks = p.get("stop_ticks") if p is not None else None
            self._managed_level = None
            self._trade_open_pnl = before_pnl  # baseline for the whole-trade P&L at close
            ctx = p["context"] if p is not None else self.last_context
            if ctx is not None:  # no context at all (fill before any bar): nothing to journal
                if cmd is not None:
                    sp, tp = self._command_brackets(cmd, fill.price)
                else:
                    sp, tp = p.get("brackets", (0.0, 0.0)) if p is not None else (0.0, 0.0)
                # The ratchet baseline for stop amendments: what NinjaTrader is actually
                # resting for this trade right now. Left None when the bracket is unknown,
                # which makes _stop_amendment fail closed rather than risk widening it.
                self.session.working_stop = sp or None
                self.tracker.on_entry(
                    ts=fill.ts, side=side, qty=abs(after_pos), price=fill.price,
                    context=ctx,
                    rationale=p["rationale"] if p is not None
                    else "unattributed_fill (no matching pending entry)",
                    confidence=p.get("confidence", 0.0) if p is not None else 0.0,
                    stop_price=sp, target_price=tp,
                    risk_reasons=p.get("risk_reasons") if p is not None else None,
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
            self._post_trade_refresh = True  # arm a post-trade re-author for the next tick
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
                self._record_breakeven_shadow(trade)
                if self.on_close is not None:
                    self.on_close(trade)
        # else: a partial REDUCE toward flat (position still open) — keep tracking; the
        # close branch journals the whole trade when it finally returns to flat. (The
        # strategy flattens before reversing, so a direct long<->short flip never occurs.)

        reason = self.session.check_daily_goal()
        if reason is None and self.cfg.risk.enforce_trailing_drawdown:
            # Trailing-drawdown backstop: halt + flatten if realized losses (incl. stop slippage)
            # dropped live equity to the MLL floor. Entries near the floor are already refused by
            # the gate's would_breach_mll projection; this catches a realized breach after a fill.
            reason = self.session.check_mll(fill.price, self.cfg.risk.mll_buffer_usd)
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
        # The RiskGate refuses to REST a level that comes within this much of the last price
        # (NinjaTrader rejects it against the live book, and a rejected amendment terminates
        # the strategy -- live 2026-08-20). Claim that same band here: inside it the level
        # cannot be a stop, so the only executable answer is out. The two rules then tile the
        # line with no gap -- exit inside the band, rest a real stop outside it.
        tick = self.cfg.instrument.tick_size or 0.25
        tol = self.cfg.risk.amend_stop_clearance_ticks * tick
        breached = (
            (side == Side.LONG and close <= level + tol)
            or (side == Side.SHORT and close >= level - tol)
        )
        if not breached:
            return None
        return Decision(
            action=Action.EXIT, confidence=0.95, qty=abs(pos),
            rationale=f"managed_stop({side.value.lower()} @{level:g}): "
                      f"breakeven/trail hit on close {close:g}",
        )

    def _stop_amendment(
        self, ctx: MarketContext, bar: Bar, armed: TradePlan | None
    ) -> OrderCommand | None:
        """A RiskGate-approved AMEND_STOP when the position's protective level has TIGHTENED.

        Both discretionary exits — the plan's ExitRule and the managed breakeven/trail stop —
        are tested once per bar CLOSE, and the bridge only ever sees completed bars. Between
        two closes nothing but the wide entry bracket is actually in the market, so a fast bar
        can run arbitrarily far past an armed level before the exit can fire. Resting the
        tighter of the two levels as a REAL stop bounds that overshoot; the close-tests stay
        exactly as they were and remain the normal path.

        None when nothing is armed, the level is no tighter than what already rests, or the
        gate refuses it — the gate is the authority, this only proposes.
        """
        pos = self.session.position
        if pos == 0:
            return None
        if self.session.working_stop is None:
            # We don't know where NinjaTrader's stop currently sits (an unattributed fill, or
            # an entry whose bracket we never saw), so nothing here can PROVE an amendment
            # tightens it — and a looser one would silently widen the trade's risk. Fail
            # closed: the resting entry bracket keeps protecting it, as it did before.
            return None
        side = Side.LONG if pos > 0 else Side.SHORT
        buf = self.cfg.strategy.plan_exit_stop_buffer_ticks
        plan_level = None
        if buf > 0 and armed is not None and armed.exit is not None:
            plan_level = plan_exit_stop_price(
                side=side, exit_below=armed.exit.exit_below,
                exit_above=armed.exit.exit_above, buffer_ticks=buf,
                tick_size=self.cfg.instrument.tick_size,
            )
        exc = self.tracker.open_excursion()
        managed = managed_stop_price(
            side=side, entry=self.session.avg_price,
            initial_stop_ticks=self._active_stop_ticks,
            mfe=exc[1] if exc is not None else 0.0,
            swing_low=ctx.swing_low, swing_high=ctx.swing_high, cfg=self.cfg,
        )
        level = tightest_stop(side, [plan_level, managed])
        if level is None:
            return None
        cmd = OrderCommand(
            id=self._new_id(), strategy_id=self.cfg.strategy_id,
            action=Action.AMEND_STOP, stop_price=level,
            reason=f"resting_stop({side.value.lower()} @{level:g})",
        )
        rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
        if not rd.approved or rd.command is None:
            return None
        self.session.working_stop = level
        return rd.command

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
