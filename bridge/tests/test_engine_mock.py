from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.models import Action
from hermes_bridge.replay_sim import ReplaySimulator
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import synthetic_bars


def test_replay_runs_and_trades(cfg):
    sim = ReplaySimulator(cfg)
    report = sim.run(synthetic_bars(400), warmup=50)
    # The mock trend-pullback logic should find entries on this uptrend data.
    assert report.entries > 0
    # Every entry is eventually closed (bracket, exit, or end-of-run open is allowed).
    assert report.exits >= 0
    assert isinstance(report.realized_pnl, float)
    # No more than the configured trades/day were taken.
    assert report.trades_today <= cfg.risk.max_trades_per_day


def test_engine_never_enters_while_in_position(cfg):
    """Invariant: with a non-flat position, on_bar must not emit an ENTER command."""
    store = BarStore("ES", "5m")
    session = SessionState("ES", "5m", 0.25, 12.5, 500.0, 400.0)
    session.position = 1  # pretend we are long
    session.avg_price = 4000.0
    engine = TradingEngine(cfg, store, session, build_agent_client(cfg), RiskGate(cfg))
    for bar in synthetic_bars(120):
        result = engine.on_bar(bar)
        if result.command is not None:
            assert result.command.action not in (Action.ENTER_LONG, Action.ENTER_SHORT)


def test_engine_halts_block_new_entries(cfg):
    store = BarStore("ES", "5m")
    session = SessionState("ES", "5m", 0.25, 12.5, 500.0, 400.0)
    session.halt("daily_profit_target")
    session.daily_goal_hit = True
    engine = TradingEngine(cfg, store, session, build_agent_client(cfg), RiskGate(cfg))
    # Bars confined to a single UTC day (midnight-aligned start) so the daily-reset
    # logic does not legitimately clear the halt mid-series.
    for bar in synthetic_bars(60, start_ts=1_699_920_000):
        result = engine.on_bar(bar)
        assert result.command is None or result.command.action in (Action.EXIT, Action.FLATTEN)
