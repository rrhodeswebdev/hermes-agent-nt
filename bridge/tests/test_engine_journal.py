from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.journal import JournalStore
from hermes_bridge.models import Bar, Fill, Side
from hermes_bridge.replay_sim import ReplaySimulator
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import synthetic_bars


def _engine(cfg, journal=None):
    return TradingEngine(cfg, BarStore("ES", "5m"),
                         SessionState("ES", "5m", 0.25, 12.5, 500, 400),
                         build_agent_client(cfg), RiskGate(cfg), journal=journal)


def test_engine_accepts_optional_journal(cfg, tmp_path):
    js = JournalStore(str(tmp_path / "j.jsonl"))
    assert _engine(cfg, js).journal is js


def test_engine_on_fill_journals_round_trip(cfg, tmp_path):
    js = JournalStore(str(tmp_path / "j.jsonl"))
    eng = _engine(cfg, js)
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    ctx = build_context(bars, ema_fast=9, ema_slow=21, atr_period=14)
    entry_px = bars[-1].close
    # Simulate the engine having approved an entry this bar:
    eng._pending_entry = {"context": ctx, "rationale": "test entry"}
    # Open long, manage one bar (MAE/MFE), then close flat.
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=entry_px, ts=bars[-1].ts))
    assert eng.session.position == 1
    eng.on_bar(Bar(ts=bars[-1].ts + 300, open=entry_px, high=entry_px + 5,
                   low=entry_px - 2, close=entry_px + 3))
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=entry_px + 3, ts=bars[-1].ts + 600))
    assert eng.session.position == 0
    recs = js.all()
    assert len(recs) == 1
    assert recs[0]["side"] == "LONG"
    assert recs[0]["rationale"] == "test entry"
    assert recs[0]["bars_held"] >= 1


def test_replay_records_closed_trades(cfg, tmp_path):
    js = JournalStore(str(tmp_path / "rj.jsonl"))
    sim = ReplaySimulator(cfg, journal=js)
    report = sim.run(synthetic_bars(400), warmup=50)
    assert report.entries > 0
    recs = js.all()
    assert len(recs) >= 1
    r = recs[0]
    assert r["side"] in ("LONG", "SHORT")
    assert "realized_pnl" in r and "mae" in r and "mfe" in r
    assert "trend" in r["entry_context"]
