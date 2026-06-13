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

from dataclasses import dataclass, field
from itertools import count

from .agent_client import AgentClient, AgentRequest
from .config import BridgeConfig
from .indicators import MarketContext, build_context
from .levels import detect_levels
from .models import AccountState, Action, Bar, Decision, Fill, Level, Mode, OrderCommand
from .plan import Planner, PlanRequest, TradePlan, evaluate_plan
from .risk import RiskGate
from .session import SessionState
from .store import BarStore

_CONTEXT_WINDOW = 200  # bars handed to indicator/context building


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
    ) -> None:
        self.cfg = config
        self.store = store
        self.session = session
        self.agent = agent
        self.risk = risk
        self.planner = planner if config.planner.enabled else None
        self._ids = count(1)

    def _new_id(self) -> str:
        return f"{self.cfg.strategy_id}-{next(self._ids)}"

    # ---- bar handling -------------------------------------------------------
    def on_bar(self, bar: Bar) -> EngineResult:
        self.session.maybe_roll_day(bar.ts)
        self.store.append(bar)
        self.session.mark_bar(bar.ts)

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
        account = self.session.account_state(mark_price=bar.close)
        mode = "manage_position" if self.session.position != 0 else "seek_entry"

        if mode == "seek_entry" and self.session.halted:
            return EngineResult(Decision(action=Action.WAIT, rationale="halted"), None, mode)

        armed = self.planner.current_plan() if self.planner is not None else None
        if self.planner is not None:
            decision = self._evaluate_armed_plan(armed, bar, mode)
        else:
            decision = self.agent.decide(
                AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account)
            )

        # Gate entries by minimum confidence (exits always honored).
        if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            if decision.confidence < self.cfg.strategy.min_confidence:
                decision = Decision(action=Action.WAIT,
                                    rationale=f"low_confidence:{decision.confidence}")

        if decision.action == Action.WAIT:
            result = EngineResult(decision, None, mode)
        else:
            cmd = self._to_command(decision)
            rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
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
        if self.planner is None or not bars:
            return
        recent = bars[-_CONTEXT_WINDOW:]
        ctx = build_context(
            recent,
            ema_fast=self.cfg.strategy.ema_fast,
            ema_slow=self.cfg.strategy.ema_slow,
            atr_period=self.cfg.strategy.atr_period,
        )
        account = self.session.account_state(mark_price=bars[-1].close)
        mode: Mode = "manage_position" if self.session.position != 0 else "seek_entry"
        self.planner.schedule_session_analysis(bars, PlanRequest(
            mode=mode, context=ctx, recent_bars=recent, account=account,
            bar_ts=bars[-1].ts, assumed_position=self.session.position,
            levels=self._levels(recent), outcome="session_start",
        ))

    def _levels(self, bars: list[Bar]) -> list[Level]:
        lc = self.cfg.levels
        if not lc.enabled:
            return []
        return detect_levels(
            bars, lookback=lc.lookback, tick_size=self.cfg.instrument.tick_size,
            merge_ticks=lc.merge_ticks, min_touches=lc.min_touches,
            max_levels=lc.max_levels,
        )

    # ---- fill handling ------------------------------------------------------
    def on_fill(self, fill: Fill) -> OrderCommand | None:
        """Apply a fill and, if it tripped the daily goal/limit while still in a
        position, return a FLATTEN command to enqueue immediately."""
        self.session.apply_fill(fill)
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
