"""Staggered distillation: a slower/deeper model compresses the full lesson corpus
into one bounded distilled.md that decision prompts read instead of raw lessons."""

import json

from hermes_bridge.config import BridgeConfig
from hermes_bridge.journal import JournalStore
from hermes_bridge.memory import LearnedStore
from hermes_bridge.reflect import Reflector


def _setup(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    learned = LearnedStore(cfg.learning.learned_dir)
    return cfg, learned, Reflector(cfg, learned, JournalStore(str(tmp_path / "j.jsonl")))


def test_distill_writes_distilled_with_deep_model(tmp_path, monkeypatch):
    cfg, learned, r = _setup(tmp_path)
    learned.apply_lesson("create", "clearance", body="Need 1xATR room to structure.")
    learned.append_note("midday chop fades signals")
    seen = {}

    def _capture(c, system, user, json_schema=None, model=None, timeout_s=None):
        seen.update(model=model, user=user, system=system)
        return json.dumps({"is_error": False,
                           "structured_output": {"distilled": "- HARD: 1xATR clearance."}})

    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot", _capture)
    applied = r.distill()
    assert applied == {"distilled": 1, "error": None}
    assert seen["model"] == "opus"                     # the slow, deep tier
    assert "1xATR room" in seen["user"]                # full lesson bodies included
    assert "midday chop" in seen["user"]
    assert "HARD: 1xATR clearance" in learned.distilled()


def test_distill_failure_is_reported(tmp_path, monkeypatch):
    cfg, learned, r = _setup(tmp_path)
    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot",
                        lambda *a, **k: "garbage")
    applied = r.distill()
    assert applied["distilled"] == 0
    assert applied["error"] == "no_distilled"
    assert learned.distilled() == ""


def test_distill_non_dict_payload_is_safe(tmp_path, monkeypatch):
    """Defensive: extract_structured is documented dict|None, but distill must still fail
    safe (never raise) if it ever yields a non-dict — these paths must never disrupt trading."""
    cfg, learned, r = _setup(tmp_path)
    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot", lambda *a, **k: "x")
    monkeypatch.setattr("hermes_bridge.reflect.extract_structured", lambda *a, **k: ["nope"])
    applied = r.distill()
    assert applied["distilled"] == 0 and applied["error"]
    assert learned.distilled() == ""


def test_distill_respects_char_limit(tmp_path, monkeypatch):
    cfg, learned, r = _setup(tmp_path)
    cfg.learning.distilled_char_limit = 20
    long_text = "x" * 500
    monkeypatch.setattr(
        "hermes_bridge.reflect.run_claude_oneshot",
        lambda *a, **k: json.dumps({"is_error": False,
                                    "structured_output": {"distilled": long_text}}))
    applied = r.distill()
    assert applied["distilled"] == 1
    # The artifact is hard-capped AT the configured limit (+1 for the trailing newline).
    assert len(learned.distilled()) <= cfg.learning.distilled_char_limit


def test_prompt_prefers_distilled_over_raw_lessons(tmp_path):
    cfg, learned, r = _setup(tmp_path)
    learned.apply_lesson("create", "raw-lesson", body="RAW LESSON BODY")
    learned.append_note("fresh note since distill")
    out = learned.format_for_prompt()
    assert "RAW LESSON BODY" in out                    # no distilled yet -> legacy path
    learned.set_distilled("- distilled rule one")
    out = learned.format_for_prompt()
    assert "DISTILLED LESSONS" in out
    assert "distilled rule one" in out
    assert "RAW LESSON BODY" not in out                # distilled replaces raw lessons
    assert "fresh note since distill" in out           # fresh notes keep flowing


def test_distill_write_is_boundary_aware(tmp_path, monkeypatch):
    cfg, learned, r = _setup(tmp_path)
    cfg.learning.distilled_char_limit = 120
    long_text = "### RULES\n" + "\n".join(f"- rule number {i} holds firmly" for i in range(30))
    monkeypatch.setattr("hermes_bridge.reflect.run_claude_oneshot", lambda *a, **k: "stubbed")

    def _extract_distilled(reply):
        return {"distilled": long_text}

    monkeypatch.setattr("hermes_bridge.reflect.extract_structured", _extract_distilled)

    applied = r.distill()
    assert applied["distilled"] == 1
    written = learned.distilled()
    assert 0 < len(written) <= 120
    # boundary-aware: ends with the marker, never mid-word
    assert written.endswith("…")
    assert "- rule number" in written
    # boundary-aware: the last kept line is a COMPLETE bullet from the source, not a
    # fragment (a raw slice + stamped marker would fail this)
    last_line = written.removesuffix("…").rstrip().splitlines()[-1]
    assert last_line.startswith("- rule number") and last_line.endswith("holds firmly")


def test_distilled_char_limit_default_raised():
    from hermes_bridge.config import LearningConfig
    assert LearningConfig().distilled_char_limit == 2400


def test_distill_input_includes_day_review_footers(tmp_path, monkeypatch):
    import hermes_bridge.reflect as reflect_mod
    from hermes_bridge.config import BridgeConfig
    from hermes_bridge.journal import JournalStore
    from hermes_bridge.memory import LearnedStore

    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    learned = LearnedStore(cfg.learning.learned_dir)
    learned.append_day_review(
        "2026-07-01",
        "Narrative text here.\n\n_theme: whipsaw_range · observation: flat day wins._",
        keep=10)
    journal = JournalStore(str(tmp_path / "journal.jsonl"))
    r = reflect_mod.Reflector(cfg, learned, journal)

    captured = {}

    def fake_oneshot(claude_cfg, system, user, **kw):
        captured["user"] = user
        return "stubbed"

    monkeypatch.setattr(reflect_mod, "run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(reflect_mod, "extract_structured", lambda reply: {"distilled": "- ok"})
    assert r.distill()["distilled"] == 1
    assert "DAY-REVIEW THEMES" in captured["user"]
    assert "_theme: whipsaw_range · observation: flat day wins._" in captured["user"]


def test_distill_survives_day_review_read_failure(tmp_path, monkeypatch):
    import hermes_bridge.reflect as reflect_mod
    from hermes_bridge.config import BridgeConfig
    from hermes_bridge.journal import JournalStore
    from hermes_bridge.memory import LearnedStore

    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    learned = LearnedStore(cfg.learning.learned_dir)
    journal = JournalStore(str(tmp_path / "journal.jsonl"))
    r = reflect_mod.Reflector(cfg, learned, journal)

    captured = {}

    def fake_oneshot(claude_cfg, system, user, **kw):
        captured["user"] = user
        return "stubbed"

    def boom(n):
        raise OSError("corrupt day-reviews")

    monkeypatch.setattr(learned, "day_reviews", boom)
    monkeypatch.setattr(reflect_mod, "run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(reflect_mod, "extract_structured", lambda reply: {"distilled": "- ok"})
    assert r.distill()["distilled"] == 1
    assert "DAY-REVIEW THEMES" not in captured["user"]
