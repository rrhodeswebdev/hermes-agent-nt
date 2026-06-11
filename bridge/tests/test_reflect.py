import json

from hermes_bridge.config import BridgeConfig
from hermes_bridge.journal import ClosedTrade, JournalStore
from hermes_bridge.memory import LearnedStore
from hermes_bridge.reflect import Reflector


def _reflector(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    learned = LearnedStore(cfg.learning.learned_dir)
    journal = JournalStore(cfg.learning.journal_path)
    return cfg, learned, journal, Reflector(cfg, learned, journal)


def _trade():
    return ClosedTrade(entry_ts=1, exit_ts=2, side="LONG", qty=1, entry_price=1, exit_price=0.5,
                       realized_pnl=-25.0, bars_held=4, mae=-2, mfe=0.5, trend="up",
                       entry_context={"trend": "up"}, rationale="faded the open")


def test_reflect_applies_proposals(tmp_path, monkeypatch):
    cfg, learned, journal, r = _reflector(tmp_path)
    proposals = {
        "lessons": [{"op": "create", "name": "dont-fade-open",
                     "regime_tags": ["trend-up"], "body": "Avoid counter-trend first 30m."}],
        "notes_append": ["MNQ choppy at open"],
        "profile_replace": None,
    }
    reply = json.dumps({"is_error": False, "structured_output": proposals})
    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot", lambda *a, **k: reply)
    applied = r.reflect_on_close(_trade(), journal.recent(20))
    assert applied["lessons"] == 1
    assert applied["notes"] == 1
    names = [ls.name for ls in learned.lessons()]
    assert "dont-fade-open" in names
    assert "MNQ choppy at open" in learned.notes()


def test_reflect_empty_proposals_writes_nothing(tmp_path, monkeypatch):
    cfg, learned, journal, r = _reflector(tmp_path)
    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot",
                        lambda *a, **k: json.dumps({"is_error": False,
                                                    "structured_output": {"lessons": []}}))
    applied = r.reflect_on_close(_trade(), [])
    assert applied == {"lessons": 0, "notes": 0, "profile": 0}
    assert learned.lessons() == []


def test_reflect_on_claude_failure_is_safe(tmp_path, monkeypatch):
    cfg, learned, journal, r = _reflector(tmp_path)
    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot",
                        lambda *a, **k: "garbage not json")
    applied = r.reflect_on_close(_trade(), [])
    assert applied == {"lessons": 0, "notes": 0, "profile": 0}
