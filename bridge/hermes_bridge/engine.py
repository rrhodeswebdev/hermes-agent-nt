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
from .config import BridgeConfig
from .indicators import MarketContext, build_context
from .journal import ClosedTrade, JournalStore, TradeTracker
from .models import Action, Bar, Decision, Fill, OrderCommand, Side
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

        if mode == "seek_entry" and self.session.halted:
            return EngineResult(Decision(action=Action.WAIT, rationale="halted"), None, mode)

        if self._prefilter is not None and mode == "seek_entry":
            pre = self._prefilter.decide(
                AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account))
            if pre.action not in (Action.ENTER_LONG, Action.ENTER_SHORT):
                return EngineResult(
                    Decision(action=Action.WAIT, rationale="prefilter:no_candidate"), None, mode)

        decision = self.agent.decide(
            AgentRequest(mode=mode, context=ctx, recent_bars=bars, account=account)
        )

        # Gate entries by minimum confidence (exits always honored).
        if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            if decision.confidence < self.cfg.strategy.min_confidence:
                return EngineResult(
                    Decision(action=Action.WAIT,
                             rationale=f"low_confidence:{decision.confidence}"),
                    None, mode,
                )

        if decision.action == Action.WAIT:
            return EngineResult(decision, None, mode)

        cmd = self._to_command(decision)
        rd = self.risk.evaluate(cmd, self.session, last_price=bar.close, now_ts=bar.ts)
        if rd.approved and rd.command is not None and decision.action in (
            Action.ENTER_LONG, Action.ENTER_SHORT
        ):
            self._pending_entry = {"context": ctx, "rationale": decision.rationale,
                                   "confidence": decision.confidence}
        return EngineResult(decision, rd.command if rd.approved else None, mode, rd.reasons)

    # ---- fill handling ------------------------------------------------------
    def on_fill(self, fill: Fill) -> OrderCommand | None:
        """Apply a fill, journal a completed trade on close, and flatten if the daily
        goal/limit tripped while still in a position."""
        before_pos = self.session.position
        before_pnl = self.session.realized_pnl
        self.session.apply_fill(fill)
        after_pos = self.session.position

        if before_pos == 0 and after_pos != 0 and self._pending_entry is not None:
            self.tracker.on_entry(
                ts=fill.ts, side=Side.LONG if after_pos > 0 else Side.SHORT,
                qty=abs(after_pos), price=fill.price,
                context=self._pending_entry["context"],
                rationale=self._pending_entry["rationale"],
                confidence=self._pending_entry.get("confidence", 0.0),
            )
            self._pending_entry = None
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
