"""Pre-armed trade plans — the between-bars analysis → trigger cycle.

The LLM never sits on the bar-close critical path. After each close, an analysis
runs in the background and arms a TradePlan: explicit price conditions for the
NEXT bar close ("if it closes at/above X, enter long with this stop/target").
When that bar closes, the engine just compares the close against the armed plan
and acts instantly — no analysis at decision time. If nothing triggers, the next
analysis is scheduled and the cycle repeats. While in a trade the engine answers
WAIT immediately (or fires a pre-armed exit rule) and the follow-up analysis
plans the next close in manage mode.

`Planner` holds the armed plan + the start-of-session brief, and owns the
background worker (latest-request-wins). `synchronous=True` runs analyses
inline, which keeps the replay harness and the tests deterministic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from .agent_client import AgentRequest
from .models import Action, Bar, BrainTimeout, Decision, FrozenModel, Level, Mode

if TYPE_CHECKING:
    from .agent_client import AgentClient
    from .config import BridgeConfig


class EntryTrigger(FrozenModel):
    """One mechanical entry condition on the NEXT bar's close price.

    Fires when `min_close <= close <= max_close` (each bound optional, at least
    one required). The bracket comes pre-computed from the analysis so no
    judgment is needed at fire time.
    """

    direction: Literal["long", "short"]
    min_close: float | None = None   # fires only if close >= this
    max_close: float | None = None   # fires only if close <= this
    qty: int = 1
    stop_ticks: int | None = None
    target_ticks: int | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    # The authored setup this trigger implements (agent mode). The bridge validates it
    # against the roster and derives the plan's active_strategy from the trigger that fires,
    # so the dashboard highlights the setup actually traded. Display only — never gates firing.
    setup: str | None = None
    rationale: str = ""
    # Plan-time risk-cap feasibility (set by the Planner when risk.shadow_infeasible_triggers
    # is on). False = the stop would bust the RiskGate's single-contract cap, so the trigger is
    # SHADOWED: shown + counterfactually replayed (over_cap bucket) but never fired (see
    # matches()). Default True so behavior is unchanged when the feature is off.
    feasible: bool = True
    infeasible_reason: str | None = None

    def matches(self, close: float) -> bool:
        if not self.feasible:
            return False  # shadowed: un-fillable at the risk cap — never fires
        if self.qty <= 0:
            return False  # a trigger that would buy 0 contracts is not a trigger
        if self.min_close is None and self.max_close is None:
            return False  # unconditional entries are not allowed
        if self.min_close is not None and close < self.min_close:
            return False
        if self.max_close is not None and close > self.max_close:
            return False
        return True

    def describe(self) -> str:
        parts = []
        if self.min_close is not None:
            parts.append(f"close>={self.min_close:g}")
        if self.max_close is not None:
            parts.append(f"close<={self.max_close:g}")
        desc = f"{self.direction}[{' and '.join(parts) or 'never'}]"
        if not self.feasible:
            desc += f" ~shadow({self.infeasible_reason or 'over_cap'})"
        return desc


class ExitRule(FrozenModel):
    """Invalidation thresholds for an open position, checked at the next close.

    The resting bracket in NinjaTrader still protects the trade intrabar; this
    rule is the pre-armed discretionary exit ("if it closes beyond X, get out").
    """

    exit_below: float | None = None  # exit if close <= this
    exit_above: float | None = None  # exit if close >= this
    setup: str | None = None         # authored setup the open position is managed under
    rationale: str = ""

    def matches(self, close: float) -> bool:
        if self.exit_below is not None and close <= self.exit_below:
            return True
        if self.exit_above is not None and close >= self.exit_above:
            return True
        return False

    def describe(self) -> str:
        parts = []
        if self.exit_below is not None:
            parts.append(f"close<={self.exit_below:g}")
        if self.exit_above is not None:
            parts.append(f"close>={self.exit_above:g}")
        return " or ".join(parts) or "none"


class TradePlan(FrozenModel):
    """What one analysis armed for the next bar close.

    `mode` and `based_on_bar_ts` are stamped by the bridge (never trusted from
    the LLM): the mode the plan was made for, and the close timestamp of the bar
    the analysis last saw — used for the staleness check.
    """

    mode: Mode = "seek_entry"
    bias: Literal["long", "short", "neutral"] = "neutral"
    # The authored setup this plan is trading. NOT taken from the LLM directly: the bridge
    # derives it from the armed trigger's (validated) `setup` so it always names a real setup
    # and matches the condition that actually fires. The dashboard highlights it. Display
    # only — never gates trading; None when no armed condition maps to a named setup.
    active_strategy: str | None = None
    triggers: list[EntryTrigger] = Field(default_factory=list)
    exit: ExitRule | None = None
    rationale: str = ""
    based_on_bar_ts: float = 0.0

    def describe_conditions(self) -> str:
        if self.mode == "manage_position":
            return self.exit.describe() if self.exit else "hold (bracket only)"
        return ", ".join(t.describe() for t in self.triggers) or "no-trade"


def describe_analysis_error(exc: Exception) -> str:
    """Dashboard-friendly error tag. Timeouts name the exceeded bridge-side budget
    (planner.plan_timeout_s / session_timeout_s) so they aren't mistaken for the
    NinjaTrader HttpTimeoutMs strategy setting."""
    if isinstance(exc, BrainTimeout):
        return f"timeout({exc.budget_s:g}s bridge budget)"
    return type(exc).__name__


def evaluate_plan(plan: TradePlan, bar: Bar, position: int) -> Decision:
    """Compare the just-closed bar against the armed plan. Pure and instant.

    Mode/staleness checks happen in the engine before this is called; here the
    plan is assumed valid for the current position state.
    """
    close = bar.close
    if plan.mode == "manage_position":
        if position != 0 and plan.exit is not None and plan.exit.matches(close):
            return Decision(
                action=Action.EXIT, confidence=0.9,
                rationale=f"plan_exit({plan.exit.describe()}): {plan.exit.rationale}",
            )
        return Decision(
            action=Action.WAIT,
            rationale=f"in_trade_hold (exit armed: "
                      f"{plan.exit.describe() if plan.exit else 'bracket only'})",
        )
    for t in plan.triggers:
        if t.matches(close):
            return Decision(
                action=Action.ENTER_LONG if t.direction == "long" else Action.ENTER_SHORT,
                confidence=t.confidence, qty=t.qty,
                stop_ticks=t.stop_ticks, target_ticks=t.target_ticks,
                rationale=f"plan_trigger({t.describe()}): {t.rationale}",
            )
    return Decision(
        action=Action.WAIT,
        rationale=f"no_trigger (armed: {plan.describe_conditions()})",
    )


@dataclass(frozen=True)
class PlanRequest(AgentRequest):
    """Snapshot handed to the between-bars analysis (built at bar close).

    Extends the per-bar `AgentRequest` (whose `mode` is the mode the NEXT plan
    should be made for) with the plan-cycle context."""

    bar_ts: float                   # close ts of the bar the analysis is based on
    assumed_position: int           # position assumed at the next close (optimistic
                                    # post-fill when an entry/exit was just queued)
    levels: list[Level] = field(default_factory=list)
    prior_plan: TradePlan | None = None
    outcome: str = ""               # what just happened at this close
    session_brief: str = ""         # filled in by the Planner at analysis time


class Planner:
    """Armed-plan state + the analysis worker that keeps it fresh.

    All analyses run off the bar-close critical path. The worker keeps only the
    latest pending request per kind (a newer bar supersedes an unstarted
    analysis), and an arriving plan never replaces one based on a newer bar.
    """

    def __init__(self, cfg: BridgeConfig, agent: AgentClient, *,
                 synchronous: bool = False) -> None:
        self.cfg = cfg
        self.agent = agent
        self.synchronous = synchronous
        self._lock = threading.Lock()
        self._plan: TradePlan | None = None
        self._consumed_ts: float | None = None  # basis ts of the last fired plan
        self._brief: str = ""
        self._status: str = "idle"   # idle|analyzing_session|analyzing|armed|consumed|error
        self._last_error: str = ""
        self._session_error: str = ""  # survives arm(); the brief failed for the day
        # background worker (lazy; latest-request-wins slots)
        self._cv = threading.Condition()
        self._pending_session: tuple[list[Bar], PlanRequest] | None = None
        self._pending_plan: PlanRequest | None = None
        self._thread: threading.Thread | None = None

    # ---- read side ----------------------------------------------------------
    def current_plan(self) -> TradePlan | None:
        with self._lock:
            return self._plan

    def session_brief(self) -> str:
        with self._lock:
            return self._brief

    def clear_session(self) -> None:
        """Drop the session brief + armed plan so the NEXT ``schedule_session_analysis``
        re-runs the pre-session study (which re-authors the playbook in agent mode) instead
        of short-circuiting. Used by ``/control/reauthor`` to refresh without a bridge
        restart. ``_consumed_ts`` is intentionally kept so a just-fired plan cannot re-arm
        and fire twice across the re-author."""
        with self._lock:
            self._brief = ""
            self._plan = None
            self._status = "idle"
            self._last_error = ""
            self._session_error = ""

    def snapshot(self) -> dict:
        with self._lock:
            plan = self._plan
            shadowed = [t for t in plan.triggers if not t.feasible] if plan else []
            return {
                "status": self._status,
                "last_error": self._last_error,
                "session_error": self._session_error,
                "session_brief_chars": len(self._brief),
                "conditions": plan.describe_conditions() if plan else None,
                "plan": plan.model_dump() if plan else None,
                "triggers_shadowed": len(shadowed),
                "shadow_reason": shadowed[-1].infeasible_reason if shadowed else None,
            }

    # ---- scheduling ---------------------------------------------------------
    def is_analyzing_session(self) -> bool:
        """True while the pre-session study is running — used to avoid stacking a second
        re-author on top of one already in flight."""
        with self._lock:
            return self._status == "analyzing_session"

    def schedule_session_analysis(self, history: list[Bar], preq: PlanRequest,
                                  *, force: bool = False) -> None:
        if not force and self.session_brief():
            # Mid-session reconnect (NinjaTrader re-enables and re-posts history):
            # the study already ran, and re-deriving it would blind the plan cycle
            # for minutes. Just refresh the plan from this snapshot. ``force=True``
            # (volatility-adaptive re-author / manual reauthor) bypasses this and
            # re-runs the study, overwriting the brief + playbook in place.
            self.schedule_plan_analysis(preq)
            return
        if self.synchronous:
            self._run_session(history, preq)
            return
        with self._cv:
            self._pending_session = (history, preq)
            self._cv.notify()
        self._ensure_thread()

    def schedule_plan_analysis(self, preq: PlanRequest) -> None:
        if self.synchronous:
            self._run_plan(preq)
            return
        with self._cv:
            self._pending_plan = preq
            self._cv.notify()
        self._ensure_thread()

    # ---- analysis runs ------------------------------------------------------
    def _run_session(self, history: list[Bar], preq: PlanRequest) -> None:
        self._set_status("analyzing_session")
        try:
            brief = self.agent.analyze_session(preq, history) or ""
        except Exception as exc:  # noqa: BLE001 — analysis failure must never crash ingest
            err = f"session_analysis:{describe_analysis_error(exc)}"
            with self._lock:
                self._session_error = err  # arm() clears last_error; this one persists
            self._set_status("error", err)
            brief = ""
        with self._lock:
            self._brief = brief.strip()
        # Arm the initial plan from the same history so the first realtime bar
        # already has trigger conditions to check.
        self._run_plan(preq)

    def _run_plan(self, preq: PlanRequest) -> None:
        self._set_status("analyzing")
        preq = replace(preq, session_brief=self.session_brief(),
                       prior_plan=self.current_plan())
        try:
            plan = self.agent.propose_plan(preq)
        except Exception as exc:  # noqa: BLE001 — report it; any previously armed plan
            # stays live until staleness retires it (graceful degradation, test-pinned)
            self._set_status("error", f"plan_analysis:{describe_analysis_error(exc)}")
            return
        if plan is None:
            self._set_status("error", "plan_analysis:no_plan_returned")
            return
        # The bridge is authoritative for mode + basis bar; never trust the LLM. Stamp them
        # by building a new plan (the models are frozen — values, not mutated in place).
        plan = plan.model_copy(update={"mode": preq.mode, "based_on_bar_ts": preq.bar_ts})
        plan = self._mark_feasibility(plan, preq)
        self.arm(plan)

    def _mark_feasibility(self, plan: TradePlan, preq: PlanRequest) -> TradePlan:
        """Shadow any entry trigger the RiskGate would reject for single-contract risk: flag
        it feasible=False (shown + counterfactually replayed, but never fired) instead of
        leaving it armed to be silently rejected at fire time. Off unless
        risk.shadow_infeasible_triggers; a no-op when nothing is over the cap."""
        if not self.cfg.risk.shadow_infeasible_triggers or not plan.triggers:
            return plan
        from .risk import trigger_feasible
        atr = preq.context.atr if preq.context is not None else None
        marked, changed = [], False
        for t in plan.triggers:
            ok, reason = trigger_feasible(self.cfg, stop_ticks=t.stop_ticks, atr=atr)
            if ok:
                marked.append(t)
            else:
                marked.append(
                    t.model_copy(update={"feasible": False, "infeasible_reason": reason})
                )
                changed = True
        return plan.model_copy(update={"triggers": marked}) if changed else plan

    def arm(self, plan: TradePlan) -> None:
        """Install a plan — unless one based on a newer bar is already armed, or an
        equally-new one already fired (a consumed plan must never re-arm and fire
        twice)."""
        with self._lock:
            if self._consumed_ts is not None and plan.based_on_bar_ts <= self._consumed_ts:
                return
            if self._plan is None or plan.based_on_bar_ts >= self._plan.based_on_bar_ts:
                self._plan = plan
                self._status = "armed"
                self._last_error = ""

    def consume(self, plan: TradePlan) -> None:
        """Disarm after the plan produced a queued order: a plan fires at most once.

        Without this, the same trigger band fires again on the next close whenever
        the fill (or the follow-up analysis, whose budget can exceed the bar period)
        is still in flight — doubling the position."""
        with self._lock:
            if self._plan is plan:
                self._plan = None
                self._consumed_ts = plan.based_on_bar_ts
                self._status = "consumed"

    def _set_status(self, status: str, error: str = "") -> None:
        with self._lock:
            self._status = status
            if error:
                self._last_error = error

    # ---- worker -------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._worker, name="hermes-planner", daemon=True
        )
        self._thread.start()

    def _worker(self) -> None:
        while True:
            with self._cv:
                while self._pending_session is None and self._pending_plan is None:
                    self._cv.wait()
                session_job = self._pending_session
                self._pending_session = None
                plan_job = None
                if session_job is None:
                    plan_job = self._pending_plan
                    self._pending_plan = None
            if session_job is not None:
                self._run_session(*session_job)
            elif plan_job is not None:
                self._run_plan(plan_job)
