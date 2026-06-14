"""Fill-time attribution of `_pending_entry` (journal hygiene for the learning loop).

The memo is stamped with cmd_id/ts/side when risk approves an entry. A fill is only
journaled under that memo's context/rationale when it plausibly came from it: same
side, recent enough. Stale-dropped commands disarm the memo via entry_dropped().
Anything else (manual fills, /agent/command, a dropped command that filled anyway)
is journaled as an unattributed fill so reflection never learns from mislabeled trades.
"""

from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.journal import JournalStore
from hermes_bridge.models import Fill, Side
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import synthetic_bars


def _engine(cfg, tmp_path):
    js = JournalStore(str(tmp_path / "j.jsonl"))
    eng = TradingEngine(cfg, BarStore("ES", "5m"),
                        SessionState("ES", "5m", 0.25, 12.5, 500, 400),
                        build_agent_client(cfg), RiskGate(cfg), journal=js)
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    eng.last_context = build_context(bars, atr_period=14)
    return eng, js, bars


def _memo(eng, ts, side=Side.LONG):
    return {"cmd_id": "cmd-1", "ts": ts, "side": side,
            "context": eng.last_context, "rationale": "armed entry", "confidence": 0.7}


def _round_trip(eng, ts, entry_side=Side.LONG):
    exit_side = Side.SHORT if entry_side == Side.LONG else Side.LONG
    eng.on_fill(Fill(side=entry_side, qty=1, price=100.0, ts=ts))
    eng.on_fill(Fill(side=exit_side, qty=1, price=100.5, ts=ts + 60))


def test_fresh_matching_memo_attributes(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    _round_trip(eng, bars[-1].ts + 10)
    recs = js.all()
    assert len(recs) == 1
    assert recs[0]["rationale"] == "armed entry"
    assert recs[0]["confidence"] == 0.7


def test_entry_dropped_clears_memo_and_fill_is_unattributed(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    eng.entry_dropped("cmd-1")
    assert eng._pending_entry is None
    _round_trip(eng, bars[-1].ts + 10)
    recs = js.all()
    assert len(recs) == 1
    assert "unattributed" in recs[0]["rationale"]


def test_entry_dropped_ignores_other_command_ids(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    eng.entry_dropped("someone-else")
    assert eng._pending_entry is not None


def test_side_mismatch_is_not_attributed(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts, side=Side.LONG)
    _round_trip(eng, bars[-1].ts + 10, entry_side=Side.SHORT)  # short fill vs long memo
    recs = js.all()
    assert len(recs) == 1
    assert recs[0]["side"] == "SHORT"
    assert "unattributed" in recs[0]["rationale"]


def test_stale_memo_is_not_attributed(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    _round_trip(eng, bars[-1].ts + 3600)  # an hour later — far past budget + one bar
    recs = js.all()
    assert len(recs) == 1
    assert "unattributed" in recs[0]["rationale"]
