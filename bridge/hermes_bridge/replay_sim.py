"""Offline replay harness — a tiny bar-by-bar simulator.

Feeds historical bars through the SAME engine + risk gate the live path uses, and
simulates NinjaTrader's fills (entry at bar close, bracket stop/target checked
against each later bar's range). This lets us verify the entire decision loop with
no NinjaTrader and no LLM (using MockAgentClient), and it doubles as the basis for
the engine tests. It is a sanity harness, not a production backtester (no slippage,
commissions, or intrabar tick sequencing).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent_client import build_agent_client
from .config import BridgeConfig
from .engine import TradingEngine
from .models import Action, Bar, Fill, Side
from .risk import RiskGate
from .session import SessionState
from .store import BarStore


@dataclass
class _Bracket:
    side: Side
    qty: int
    stop_price: float
    target_price: float


@dataclass
class ReplayReport:
    decisions: list[str] = field(default_factory=list)
    entries: int = 0
    exits: int = 0
    realized_pnl: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""

    def summary(self) -> str:
        return (
            f"entries={self.entries} exits={self.exits} "
            f"realized_pnl={self.realized_pnl:.2f} trades_today={self.trades_today} "
            f"halted={self.halted}({self.halt_reason})"
        )


class ReplaySimulator:
    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config
        self.store = BarStore(config.instrument.symbol, config.instrument.timeframe)
        self.session = SessionState(
            instrument=config.instrument.symbol,
            timeframe=config.instrument.timeframe,
            tick_size=config.instrument.tick_size,
            tick_value=config.instrument.tick_value,
            profit_target=config.daily_goal.profit_target,
            max_daily_loss=config.daily_goal.max_daily_loss,
        )
        self.engine = TradingEngine(
            config, self.store, self.session,
            build_agent_client(config), RiskGate(config),
        )
        self._bracket: _Bracket | None = None

    def run(self, bars: list[Bar], warmup: int = 50) -> ReplayReport:
        report = ReplayReport()
        if warmup > 0:
            self.store.replace_history(bars[:warmup])
        for bar in bars[warmup:]:
            # 1) Resolve any resting bracket against this bar's range first.
            self._check_bracket(bar, report)
            # 2) Run the engine on the closed bar.
            result = self.engine.on_bar(bar)
            report.decisions.append(f"{int(bar.ts)} {result.decision.action} "
                                    f"[{result.mode}] {result.decision.rationale}")
            cmd = result.command
            if cmd is None:
                continue
            if cmd.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
                self._open(cmd.action, cmd.qty, bar, cmd.stop_ticks, cmd.target_ticks,
                           cmd.stop_price, cmd.target_price, report)
            elif cmd.action in (Action.EXIT, Action.FLATTEN):
                self._close(bar.close, bar.ts, report)
        report.realized_pnl = self.session.realized_pnl
        report.trades_today = self.session.trades_today
        report.halted = self.session.halted
        report.halt_reason = self.session.halt_reason
        return report

    # ---- fill simulation ----------------------------------------------------
    def _open(self, action, qty, bar, stop_ticks, target_ticks, stop_price, target_price,
              report: ReplayReport) -> None:
        entry = bar.close
        ts = self.cfg.instrument.tick_size
        side = Side.LONG if action == Action.ENTER_LONG else Side.SHORT
        if side == Side.LONG:
            sp = stop_price if stop_price is not None else entry - (stop_ticks or 0) * ts
            tp = target_price if target_price is not None else entry + (target_ticks or 0) * ts
        else:
            sp = stop_price if stop_price is not None else entry + (stop_ticks or 0) * ts
            tp = target_price if target_price is not None else entry - (target_ticks or 0) * ts
        self._apply(Fill(side=side, qty=qty, price=entry, ts=bar.ts))
        self._bracket = _Bracket(side=side, qty=qty, stop_price=sp, target_price=tp)
        report.entries += 1

    def _check_bracket(self, bar: Bar, report: ReplayReport) -> None:
        b = self._bracket
        if b is None:
            return
        exit_price: float | None = None
        if b.side == Side.LONG:
            if bar.low <= b.stop_price:      # stop first (conservative)
                exit_price = b.stop_price
            elif bar.high >= b.target_price:
                exit_price = b.target_price
        else:
            if bar.high >= b.stop_price:
                exit_price = b.stop_price
            elif bar.low <= b.target_price:
                exit_price = b.target_price
        if exit_price is not None:
            self._close(exit_price, bar.ts, report)

    def _close(self, price: float, ts: float, report: ReplayReport) -> None:
        if self.session.position == 0:
            self._bracket = None
            return
        closing_side = Side.SHORT if self.session.position > 0 else Side.LONG
        qty = abs(self.session.position)
        self._apply(Fill(side=closing_side, qty=qty, price=price, ts=ts))
        self._bracket = None
        report.exits += 1

    def _apply(self, fill: Fill) -> None:
        # Drive session accounting AND the engine's post-fill goal check.
        follow_up = self.engine.on_fill(fill)
        if follow_up is not None and follow_up.action == Action.FLATTEN:
            # Goal/limit tripped mid-trade; close the rest immediately at fill price.
            if self.session.position != 0:
                closing_side = Side.SHORT if self.session.position > 0 else Side.LONG
                self.session.apply_fill(
                    Fill(side=closing_side, qty=abs(self.session.position),
                         price=fill.price, ts=fill.ts)
                )
            self._bracket = None
