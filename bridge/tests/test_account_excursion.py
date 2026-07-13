"""The open-trade excursion the brain SEES while managing a position.

`AccountState` gains mfe/mae/giveback in points, populated by the engine from the live
TradeTracker whenever a position is open, so the between-bars analysis can reason about a
winner round-tripping — something a fixed bracket + breakeven trail can't express. These
cover the ENGINE wiring (tracker MFE -> enriched account) and that the fields actually reach
the brain's prompt (build_user_prompt dumps the account wholesale).
"""

import json

from hermes_bridge.agent_client import AgentRequest, MockAgentClient, build_user_prompt
from hermes_bridge.config import (
    BridgeConfig,
    DailyGoal,
    InstrumentConfig,
    RiskParams,
    StrategyParams,
)
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.models import Action, Decision, Side
from hermes_bridge.risk import RiskGate
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, make_session


class _HoldAgent(MockAgentClient):
    """Always WAIT, so nothing the brain does can close the position during on_bar."""

    def decide(self, req):
        return Decision(action=Action.WAIT, rationale="hold")


def _cfg(**strat) -> BridgeConfig:
    base = dict(atr_period=14, swing_lookback=3, min_confidence=0.55,
                breakeven_r=1.0, trail_enabled=False)
    base.update(strat)
    return BridgeConfig(
        strategy_id="test-es",
        instrument=InstrumentConfig(symbol="ES", timeframe="5m", tick_size=0.25,
                                    tick_value=12.5),
        strategy=StrategyParams(**base),
        risk=RiskParams(max_contracts=2, max_risk_per_trade=250.0, default_stop_ticks=16),
        daily_goal=DailyGoal(profit_target=500.0, max_daily_loss=400.0),
    )


def _flat_seed(entry: float, n: int = 80, start_ts: int = 1_700_000_000) -> list:
    """Flat ±0.75pt zigzag at `entry` so ATR > 0 and swings hug the entry price."""
    bars = []
    for i in range(n):
        mid = entry + (0.75 if i % 2 else -0.75)
        bars.append(make_bar(start_ts + i * 300, mid, mid + 1.0, mid - 1.0, mid))
    return bars


def _engine_with_position(cfg, entry: float, side: Side, stop_ticks: int = 8):
    store = BarStore("ES", "5m")
    seed = _flat_seed(entry)
    store.replace_history(seed)
    session = make_session(cfg)
    session.position = 1 if side is Side.LONG else -1
    session.avg_price = entry
    engine = TradingEngine(cfg, store, session, _HoldAgent(cfg), RiskGate(cfg))
    engine._active_stop_ticks = stop_ticks
    ctx = build_context(seed, atr_period=cfg.strategy.atr_period,
                        swing_lookback=cfg.strategy.swing_lookback)
    engine.tracker.on_entry(ts=seed[-1].ts, side=side, qty=1, price=entry,
                            context=ctx, rationale="entry")
    return engine, seed[-1].ts


def test_account_for_brain_is_none_when_flat():
    cfg = _cfg()
    store = BarStore("ES", "5m")
    store.replace_history(_flat_seed(4000.0))
    engine = TradingEngine(cfg, store, make_session(cfg), _HoldAgent(cfg), RiskGate(cfg))
    dumped = engine._account_for_brain(4000.0).model_dump()
    assert dumped["mfe_points"] is None
    assert dumped["mae_points"] is None
    assert dumped["giveback_points"] is None


def test_account_for_brain_carries_mfe_and_giveback_long():
    cfg = _cfg()  # 1R = 8 * 0.25 = 2.0pt; breakeven arms at +1R, so +1.5 stays pre-managed
    entry = 4000.0
    engine, ts = _engine_with_position(cfg, entry, Side.LONG, stop_ticks=8)
    # Runs +1.5pt at the high, closes back at +0.5 → peak 1.5, currently +0.5 → gave back 1.0.
    r = engine.on_bar(make_bar(ts + 300, entry, entry + 1.5, entry - 0.25, entry + 0.5))
    assert r.command is None  # < +1R, no managed exit; the position is still open
    dumped = engine._account_for_brain(entry + 0.5).model_dump()
    assert dumped["mfe_points"] == 1.5
    assert dumped["giveback_points"] == 1.0
    assert dumped["mae_points"] <= 0.0


def test_account_for_brain_carries_mfe_short():
    cfg = _cfg()
    entry = 4000.0
    engine, ts = _engine_with_position(cfg, entry, Side.SHORT, stop_ticks=8)
    # Short favorable = price DOWN: low -1.5pt, close -0.5 → peak 1.5, gave back 1.0.
    engine.on_bar(make_bar(ts + 300, entry, entry + 0.25, entry - 1.5, entry - 0.5))
    dumped = engine._account_for_brain(entry - 0.5).model_dump()
    assert dumped["mfe_points"] == 1.5
    assert dumped["giveback_points"] == 1.0


def test_build_user_prompt_surfaces_excursion_to_the_brain():
    cfg = _cfg()
    bars = _flat_seed(4000.0, n=40)
    ctx = build_context(bars, atr_period=cfg.strategy.atr_period,
                        swing_lookback=cfg.strategy.swing_lookback)
    account = make_session(cfg).account_state(mark_price=bars[-1].close).model_copy(
        update={"mfe_points": 12.5, "mae_points": -3.0, "giveback_points": 9.0})
    req = AgentRequest(mode="manage_position", context=ctx, recent_bars=bars, account=account)
    payload = json.loads(build_user_prompt(req).split("\n", 1)[1])
    assert payload["account"]["mfe_points"] == 12.5
    assert payload["account"]["giveback_points"] == 9.0
