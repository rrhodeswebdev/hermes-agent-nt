"""Counterfactual self-correction loop — the mechanism-agnostic half.

These cover the decline log (resolved-counterfactual JSONL + unreported-win tracking),
the flat-only "missed" reflection that reads it, and the AppState trigger that fires the
reflection only while flat and above threshold. The engine-side replay that PRODUCES the
records (declined candidates / unfilled plans replayed against later bars) is tested in
the engine PR; here the records are appended directly.
"""

import json
import threading

from hermes_bridge.config import BridgeConfig
from hermes_bridge.journal import DeclineLog, JournalStore
from hermes_bridge.memory import LearnedStore
from hermes_bridge.reflect import Reflector
from hermes_bridge.server import AppState


def test_decline_log_unreported_roundtrip(tmp_path):
    log = DeclineLog(str(tmp_path / "d.jsonl"))
    log.append({"outcome": "would_win", "kind": "declined"})
    log.append({"outcome": "would_lose", "kind": "declined"})
    assert len(log.unreported_wins()) == 1
    assert len(log.recent(10)) == 2
    log.clear_unreported()
    assert log.unreported_wins() == []
    assert len(log.all()) == 2           # the JSONL record is permanent


def test_reflect_on_missed_includes_declines(tmp_path, monkeypatch):
    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    r = Reflector(cfg, LearnedStore(cfg.learning.learned_dir),
                  JournalStore(str(tmp_path / "j.jsonl")))
    seen = {}

    def _capture(c, system, user, **kw):
        seen["user"] = user
        return json.dumps({"is_error": False, "structured_output": {"lessons": []}})

    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot", _capture)
    applied = r.reflect_on_missed(
        [{"outcome": "would_win", "rationale": "clearance lesson"}], [])
    assert applied["error"] is None
    assert "NO TRADE CLOSED" in seen["user"]
    assert "clearance lesson" in seen["user"]


def test_missed_trigger_fires_only_flat_and_above_threshold(tmp_path, monkeypatch):
    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    cfg.learning.declines_path = str(tmp_path / "d.jsonl")
    cfg.learning.reflect_missed_wins = 2
    st = AppState(cfg)
    ran = threading.Event()
    monkeypatch.setattr(
        "hermes_bridge.reflect.Reflector.reflect_on_missed",
        lambda self, declines, recent: (ran.set(), {"lessons": 0})[-1])

    st.declines.append({"outcome": "would_win", "kind": "declined"})
    st.maybe_reflect_missed()                       # 1 win < threshold 2
    assert not ran.wait(0.2)

    st.declines.append({"outcome": "would_win", "kind": "declined"})
    st.session.position = 1
    st.maybe_reflect_missed()                       # in a position: deferred
    assert not ran.wait(0.2)

    st.session.position = 0
    st.maybe_reflect_missed()                       # flat + threshold met
    assert ran.wait(2)
    assert st.declines.unreported_wins() == []      # marked reported (no double-fire)


def _win(ts):
    return {"kind": "missed_trigger", "outcome": "would_win", "resolved_ts": ts,
            "side": "LONG", "entry_price": 30000.0}


def test_unreported_wins_survive_restart(tmp_path):
    from hermes_bridge.journal import DeclineLog
    p = str(tmp_path / "declines.jsonl")
    log = DeclineLog(p)
    log.append(_win(100.0))
    log.append({"kind": "missed_trigger", "outcome": "would_lose", "resolved_ts": 101.0})
    log.append(_win(102.0))
    # simulate a bridge restart: a fresh instance over the same path
    log2 = DeclineLog(p)
    assert [r["resolved_ts"] for r in log2.unreported_wins()] == [100.0, 102.0]


def test_take_unreported_advances_watermark_across_restart(tmp_path):
    from hermes_bridge.journal import DeclineLog
    p = str(tmp_path / "declines.jsonl")
    log = DeclineLog(p)
    log.append(_win(100.0))
    log.append(_win(102.0))
    assert len(log.take_unreported()) == 2
    log.append(_win(103.0))  # resolved after the drain
    log3 = DeclineLog(p)
    assert [r["resolved_ts"] for r in log3.unreported_wins()] == [103.0]


def test_corrupt_watermark_reseeds_everything(tmp_path):
    from hermes_bridge.journal import DeclineLog
    p = tmp_path / "declines.jsonl"
    log = DeclineLog(str(p))
    log.append(_win(100.0))
    log.take_unreported()
    p.with_suffix(".watermark.json").write_text("{not json", encoding="utf-8")
    log2 = DeclineLog(str(p))
    assert len(log2.unreported_wins()) == 1  # duplicate over lost — by design
