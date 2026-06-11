"""Shared fixtures + a deterministic synthetic bar generator for tests."""

from __future__ import annotations

import math

import pytest

from hermes_bridge.config import (
    BridgeConfig,
    DailyGoal,
    InstrumentConfig,
    RiskParams,
    StrategyParams,
)
from hermes_bridge.models import Bar


@pytest.fixture
def cfg() -> BridgeConfig:
    return BridgeConfig(
        strategy_id="test-es",
        instrument=InstrumentConfig(symbol="ES", timeframe="5m", tick_size=0.25, tick_value=12.5),
        strategy=StrategyParams(ema_fast=9, ema_slow=21, atr_period=14, min_confidence=0.55),
        risk=RiskParams(
            max_contracts=2, max_risk_per_trade=250.0, max_trades_per_day=10,
            default_stop_ticks=16,
        ),
        daily_goal=DailyGoal(profit_target=500.0, max_daily_loss=400.0),
    )


def make_bar(ts: float, o: float, h: float, low: float, c: float, v: float = 1000.0) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v)


def synthetic_bars(n: int = 400, start_ts: int = 1_700_000_000, step: int = 300) -> list[Bar]:
    """Uptrend with periodic pullbacks toward the moving averages."""
    bars: list[Bar] = []
    for i in range(n):
        base = 4000.0 + i * 0.5 + math.sin(i / 8.0) * 6.0
        drift = 1.0 if math.cos(i / 8.0) > 0 else -0.8
        o = base
        c = base + drift
        h = max(o, c) + 1.0
        low = min(o, c) - 1.0
        bars.append(make_bar(start_ts + i * step, o, h, low, c, 1000 + (i % 5) * 50))
    return bars


@pytest.fixture
def bars() -> list[Bar]:
    return synthetic_bars()
