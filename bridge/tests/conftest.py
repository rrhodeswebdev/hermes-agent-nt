"""Shared fixtures + a deterministic synthetic bar generator for tests."""

from __future__ import annotations

import math
import types

import pytest

from hermes_bridge.agent_client import AgentRequest
from hermes_bridge.claude_agent import ClaudeAgentClient
from hermes_bridge.config import (
    BridgeConfig,
    DailyGoal,
    InstrumentConfig,
    RiskParams,
    StrategyParams,
)
from hermes_bridge.indicators import build_context
from hermes_bridge.models import Bar
from hermes_bridge.session import SessionState


@pytest.fixture
def cfg() -> BridgeConfig:
    return BridgeConfig(
        strategy_id="test-es",
        instrument=InstrumentConfig(symbol="ES", timeframe="5m", tick_size=0.25, tick_value=12.5),
        strategy=StrategyParams(atr_period=14, swing_lookback=3, min_confidence=0.55),
        risk=RiskParams(
            max_contracts=2, max_risk_per_trade=250.0, max_trades_per_day=10,
            default_stop_ticks=16,
        ),
        daily_goal=DailyGoal(profit_target=500.0, max_daily_loss=400.0),
    )


def make_bar(ts: float, o: float, h: float, low: float, c: float, v: float = 1000.0) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v)


def make_close_bar(ts: float, close: float) -> Bar:
    """A bar whose only interesting feature is its close (plan-trigger tests)."""
    return make_bar(ts, close - 1, close + 1, close - 2, close)


def make_range_bar(ts: float, hi: float, lo: float) -> Bar:
    """A bar whose only interesting features are its extremes (pivot/level tests)."""
    mid = (hi + lo) / 2
    return make_bar(ts, mid, hi, lo, mid)


def synthetic_bars(n: int = 400, start_ts: int = 1_700_000_000, step: int = 300) -> list[Bar]:
    """Uptrend with frequent pullbacks: a steady +0.5/bar drift plus a ~19-bar oscillation,
    so even short windows print several higher-highs/higher-lows (readable swing structure)
    and regular pullbacks for the rules client to enter on."""
    bars: list[Bar] = []
    for i in range(n):
        base = 4000.0 + i * 0.5 + math.sin(i / 3.0) * 6.0
        drift = 1.0 if math.cos(i / 3.0) > 0 else -0.8
        o = base
        c = base + drift
        h = max(o, c) + 1.0
        low = min(o, c) - 1.0
        bars.append(make_bar(start_ts + i * step, o, h, low, c, 1000 + (i % 5) * 50))
    return bars


@pytest.fixture
def bars() -> list[Bar]:
    return synthetic_bars()


def make_session(cfg: BridgeConfig) -> SessionState:
    return SessionState(cfg.instrument.symbol, cfg.instrument.timeframe,
                        cfg.instrument.tick_size, cfg.instrument.tick_value,
                        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss)


def make_agent_request(cfg: BridgeConfig, mode: str = "seek_entry",
                       bars: list[Bar] | None = None) -> AgentRequest:
    bars = bars if bars is not None else synthetic_bars(120)
    ctx = build_context(bars, atr_period=cfg.strategy.atr_period,
                        swing_lookback=cfg.strategy.swing_lookback)
    return AgentRequest(mode=mode, context=ctx, recent_bars=bars,
                        account=make_session(cfg).account_state(mark_price=bars[-1].close))


def make_claude_client() -> ClaudeAgentClient:
    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    return ClaudeAgentClient(cfg)


@pytest.fixture
def fake_claude(monkeypatch):
    """Stub the `claude` subprocess behind run_claude_oneshot.

    Call the fixture to install the stub; it returns the capture dict that records
    the last invocation (cmd/input/env/timeout). Pass `exc` to make the call raise.
    """

    def install(stdout: str = "OUT", exc: Exception | None = None,
                returncode: int = 0, stderr: str = "") -> dict:
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            captured["env"] = kwargs.get("env")
            captured["timeout"] = kwargs.get("timeout")
            if exc is not None:
                raise exc
            return types.SimpleNamespace(stdout=stdout, stderr=stderr,
                                         returncode=returncode)

        monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.run", fake_run)
        return captured

    return install
