from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.journal import JournalStore
from hermes_bridge.models import Action, Bar, Fill, Side
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import synthetic_bars


def _engine(cfg, journal=None, on_close=None):
    return TradingEngine(cfg, BarStore("ES", "5m"),
                         SessionState("ES", "5m", 0.25, 12.5, 500, 400),
                         build_agent_client(cfg), RiskGate(cfg),
                         journal=journal, on_close=on_close)


def test_on_close_fires_with_closed_trade(cfg, tmp_path):
    seen = []
    eng = _engine(cfg, JournalStore(str(tmp_path / "j.jsonl")), on_close=seen.append)
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    ctx = build_context(bars, ema_fast=9, ema_slow=21, atr_period=14)
    px = bars[-1].close
    eng._pending_entry = {"context": ctx, "rationale": "r"}
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=px, ts=bars[-1].ts))
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=px + 1, ts=bars[-1].ts + 60))
    assert len(seen) == 1
    assert seen[0].side == "LONG"


def test_prefilter_mock_skips_claude_on_no_setup(cfg, monkeypatch):
    cfg.agent.client = "claude"
    cfg.agent.prefilter = "mock"
    eng = _engine(cfg)

    def boom(_req):
        raise AssertionError("claude agent should not be called when prefilter says WAIT")

    monkeypatch.setattr(eng.agent, "decide", boom)
    res = eng.on_bar(Bar(ts=1_700_000_000, open=100, high=100.1, low=99.9, close=100.0))
    assert res.decision.action is Action.WAIT
