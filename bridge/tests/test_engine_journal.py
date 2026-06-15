from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.journal import JournalStore
from hermes_bridge.models import Action, Bar, Decision, Fill, Side
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
    ctx = build_context(bars, atr_period=14)
    entry_px = bars[-1].close
    # Simulate the engine having approved an entry this bar (full memo shape — the
    # fill-time matcher checks cmd_id/ts/side before attributing the trade):
    eng._pending_entry = {"cmd_id": "test-1", "ts": bars[-1].ts, "side": Side.LONG,
                          "context": ctx, "rationale": "test entry"}
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


def test_engine_journals_partial_fill_round_trip(cfg, tmp_path):
    """A 2-lot position that fills AND exits in 1-lot legs must journal as ONE trade at
    FULL qty with the WHOLE-trade P&L — not the first entry leg's size / last exit leg's
    P&L. Reproduces the live NT8 partial-fill under-count (a 2-lot booked as 1 lot)."""
    js = JournalStore(str(tmp_path / "pj.jsonl"))
    eng = _engine(cfg, js)
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    ctx = build_context(bars, atr_period=14)
    entry_px = bars[-1].close
    eng._pending_entry = {"cmd_id": "p-1", "ts": bars[-1].ts, "side": Side.LONG,
                          "context": ctx, "rationale": "partial entry"}
    # Enter LONG 2 across two 1-lot fills (0 -> 1 -> 2).
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=entry_px, ts=bars[-1].ts))
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=entry_px, ts=bars[-1].ts))
    assert eng.session.position == 2
    eng.on_bar(Bar(ts=bars[-1].ts + 300, open=entry_px, high=entry_px + 5,
                   low=entry_px - 2, close=entry_px + 3))
    # Exit LONG 2 across two 1-lot fills at +3 pts (2 -> 1 -> 0).
    exit_px = entry_px + 3
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=exit_px, ts=bars[-1].ts + 600))
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=exit_px, ts=bars[-1].ts + 600))
    assert eng.session.position == 0
    recs = js.all()
    assert len(recs) == 1
    r = recs[0]
    assert r["side"] == "LONG"
    assert r["qty"] == 2, "multi-fill entry must journal full size, not the first leg"
    point_value = 12.5 / 0.25  # ES test instrument
    assert r["realized_pnl"] == round(2 * 3 * point_value, 2), \
        "multi-fill exit must journal the whole-trade P&L, not the last leg"


def test_suppress_transitional_gate():
    """The deterministic transitional->WAIT belt: ENTRIES are suppressed in a transitional
    regime when enabled; trending/disabled pass through, and exits are never gated."""
    enter = Decision(action=Action.ENTER_LONG, confidence=0.8, rationale="x")
    g = TradingEngine._suppress_transitional(enter, "transitional", True)
    assert g.action == Action.WAIT and "transitional" in g.rationale
    assert TradingEngine._suppress_transitional(
        enter, "trending", True).action == Action.ENTER_LONG
    assert TradingEngine._suppress_transitional(
        enter, "transitional", False).action == Action.ENTER_LONG  # neutral default
    ex = Decision(action=Action.EXIT, confidence=0.9, rationale="e")
    assert TradingEngine._suppress_transitional(ex, "transitional", True).action == Action.EXIT
