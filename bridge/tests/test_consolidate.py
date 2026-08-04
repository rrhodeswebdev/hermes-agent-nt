"""Automatic consolidation cadence: mtime material-gate, consolidate_once, status."""

import os

from hermes_bridge.memory import LearnedStore


def _touch(path, t: float) -> None:
    """Create `path` (and parents) if missing, then force its mtime to `t`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x", encoding="utf-8")
    os.utime(path, (t, t))


def test_mtime_helpers_zero_when_absent(tmp_path):
    ls = LearnedStore(str(tmp_path / "learned"))
    assert ls.distilled_mtime() == 0.0
    assert ls.lessons_mtime() == 0.0
    assert ls.corpus_mtime() == 0.0


def test_corpus_mtime_is_newest_of_inputs(tmp_path):
    ls = LearnedStore(str(tmp_path / "learned"))
    _touch(ls.dir / "agent-notes.md", 1000)
    _touch(ls.dir / "lessons" / "a.md", 2000)
    _touch(ls.dir / "distilled.md", 1500)
    assert ls.lessons_mtime() == 2000
    assert ls.distilled_mtime() == 1500
    assert ls.corpus_mtime() == 2000   # newest of notes(1000) + lessons(2000)


from hermes_bridge.config import BridgeConfig  # noqa: E402
from hermes_bridge.journal import JournalStore  # noqa: E402
from hermes_bridge.reflect import Reflector  # noqa: E402


def _reflector(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    learned = LearnedStore(cfg.learning.learned_dir)
    return cfg, learned, Reflector(cfg, learned, JournalStore(str(tmp_path / "j.jsonl")))


def _stub(r, monkeypatch):
    """Replace curate/distill with counters so consolidate_once makes no CLI call."""
    calls = {"curate": 0, "distill": 0}

    def _c():
        calls["curate"] += 1
        return {"lessons": 1, "notes": 0, "profile": 0}

    def _d():
        calls["distill"] += 1
        return {"distilled": 1, "error": None}

    monkeypatch.setattr(r, "curate", _c)
    monkeypatch.setattr(r, "distill", _d)
    return calls


def test_consolidate_skips_when_nothing_new(tmp_path, monkeypatch):
    cfg, learned, r = _reflector(tmp_path)
    _touch(learned.dir / "lessons" / "x.md", 1000)
    _touch(learned.dir / "distilled.md", 2000)   # distilled newer than the whole corpus
    r._last_curate_ts = 1000                      # lessons already tidied
    calls = _stub(r, monkeypatch)
    out = r.consolidate_once(now=9_000_000_000.0)
    assert calls == {"curate": 0, "distill": 0}
    assert out["skipped"] == "no_new_material"
    assert r._last_check_ts == 9_000_000_000.0


def test_consolidate_distills_when_corpus_newer(tmp_path, monkeypatch):
    cfg, learned, r = _reflector(tmp_path)
    _touch(learned.dir / "lessons" / "x.md", 1000)
    _touch(learned.dir / "distilled.md", 1500)
    _touch(learned.dir / "agent-notes.md", 2000)  # a note newer than distilled
    r._last_curate_ts = 1000
    calls = _stub(r, monkeypatch)
    out = r.consolidate_once(now=3000.0)
    assert calls == {"curate": 0, "distill": 1}    # notes changed -> distill, not curate
    assert out["curated"] == 0 and out["distilled"] == 1 and out["skipped"] is None


def test_consolidate_curates_then_distills_when_lessons_changed(tmp_path, monkeypatch):
    cfg, learned, r = _reflector(tmp_path)
    _touch(learned.dir / "lessons" / "x.md", 2000)  # lessons changed since last tidy
    _touch(learned.dir / "distilled.md", 1500)
    r._last_curate_ts = 1000
    calls = _stub(r, monkeypatch)
    out = r.consolidate_once(now=3000.0)
    assert calls == {"curate": 1, "distill": 1}
    assert out["curated"] == 1 and out["distilled"] == 1
    assert r._last_curate_ts == learned.lessons_mtime()  # advanced to post-curate mtime


def test_consolidate_distills_when_distilled_missing(tmp_path, monkeypatch):
    cfg, learned, r = _reflector(tmp_path)
    _touch(learned.dir / "agent-notes.md", 1000)
    assert not (learned.dir / "distilled.md").exists()
    calls = _stub(r, monkeypatch)
    r.consolidate_once(now=2000.0)
    assert calls["distill"] == 1


def test_consolidation_status_fields(tmp_path):
    cfg, learned, r = _reflector(tmp_path)
    s0 = r.consolidation_status(now=5000.0)
    assert s0["enabled"] is False           # default-neutral config
    assert s0["check_age_s"] is None        # daemon never started
    assert s0["distilled_age_s"] is None    # no distilled.md
    _touch(learned.dir / "distilled.md", 4000)
    r.mark_alive(4900.0)
    s1 = r.consolidation_status(now=5000.0)
    assert s1["check_age_s"] == 100.0
    assert s1["distilled_age_s"] == 1000.0


from fastapi.testclient import TestClient  # noqa: E402

from hermes_bridge.server import create_app  # noqa: E402


def test_dashboard_and_panel_consolidate_disabled(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.reflect_enabled = False
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    c = TestClient(create_app(cfg))
    d = c.get("/dashboard").json()
    assert d["consolidate"]["enabled"] is False
    assert d["consolidate"]["check_age_s"] is None
    assert "consolidate_enabled=0" in c.get("/panel.txt").text


def test_dashboard_and_panel_consolidate_enabled(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.reflect_enabled = False
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    cfg.learning.consolidate_enabled = True
    cfg.learning.consolidate_startup_delay_s = 3600.0  # keep the daemon idle (no CLI) in-test
    c = TestClient(create_app(cfg))
    d = c.get("/dashboard").json()
    assert d["consolidate"]["enabled"] is True            # reflects the config flag
    assert "consolidate_enabled=1" in c.get("/panel.txt").text
    # (the liveness heartbeat is the daemon's job — asserted in the Task 5 daemon tests)


def test_daemon_stamps_heartbeat_when_enabled(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.reflect_enabled = False
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    cfg.learning.consolidate_enabled = True
    cfg.learning.consolidate_startup_delay_s = 3600.0  # daemon idles in the grace, no CLI
    c = TestClient(create_app(cfg))
    # The daemon stamps _last_check_ts synchronously at startup, so liveness is immediate.
    assert c.get("/dashboard").json()["consolidate"]["check_age_s"] is not None


def test_daemon_not_started_when_disabled(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.reflect_enabled = False
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    c = TestClient(create_app(cfg))  # consolidate_enabled defaults False
    assert c.get("/dashboard").json()["consolidate"]["check_age_s"] is None


# --- Resilience: a lesson file rotating into .history/ mid-pass must not stall the
# heartbeat. Observed live 2026-07-29: `[consolidate] error: FileNotFoundError` left
# check_age_s growing unbounded while the daemon thread was alive and retrying, so
# "dormant" and "erroring" were indistinguishable from the outside.
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


def _boom(*_a, **_k):
    raise FileNotFoundError("lesson rotated into .history/ mid-iteration")


def test_heartbeat_advances_even_when_the_pass_raises(tmp_path, monkeypatch):
    """The liveness stamp must survive a failing pass — otherwise an erroring
    consolidator is indistinguishable from a dead one."""
    cfg, learned, r = _reflector(tmp_path)
    _touch(learned.dir / "lessons" / "x.md", 2000)
    _touch(learned.dir / "distilled.md", 1500)
    r._last_curate_ts = 1000
    monkeypatch.setattr(r, "curate", _boom)
    with pytest.raises(FileNotFoundError):
        r.consolidate_once(now=3000.0)          # still propagates -> server logs it
    assert r._last_check_ts == 3000.0           # ...but the heartbeat advanced


def test_lessons_mtime_tolerates_a_file_vanishing(tmp_path, monkeypatch):
    ls = LearnedStore(str(tmp_path / "learned"))
    _touch(ls.dir / "lessons" / "real.md", 2000)
    ghost = ls.dir / "lessons" / "ghost.md"     # globbed, then deleted before stat()
    real = ls.dir / "lessons" / "real.md"
    monkeypatch.setattr(Path, "glob", lambda self, pat: iter([ghost, real]))
    assert ls.lessons_mtime() == 2000


def test_lessons_tolerates_a_file_vanishing(tmp_path, monkeypatch):
    ls = LearnedStore(str(tmp_path / "learned"))
    (ls.dir / "lessons").mkdir(parents=True, exist_ok=True)
    real = ls.dir / "lessons" / "real.md"
    real.write_text("body text\n", encoding="utf-8")
    ghost = ls.dir / "lessons" / "ghost.md"
    monkeypatch.setattr(Path, "glob", lambda self, pat: iter([ghost, real]))
    out = ls.lessons()
    assert len(out) == 1                        # ghost skipped, not a FileNotFoundError


def test_curate_survives_a_lessons_read_error(tmp_path, monkeypatch):
    cfg, learned, r = _reflector(tmp_path)
    monkeypatch.setattr(learned, "lessons", _boom)
    assert r.curate() == {"lessons": 0, "notes": 0, "profile": 0}


def test_distill_survives_a_corpus_read_error(tmp_path, monkeypatch):
    cfg, learned, r = _reflector(tmp_path)
    monkeypatch.setattr(learned, "lessons", _boom)
    out = r.distill()                           # must not reach the CLI, must not raise
    assert out["distilled"] == 0
    assert out["error"] == "FileNotFoundError"


# --- Lesson filenames must fit the filesystem. The brain emitted a 373-char lesson name
# on 2026-08-03; slugged to "<373 chars>.md.tmp" that is 380 chars against the NTFS
# 255-char per-component limit, and Windows reports an over-length path as ENOENT. The
# write raised out of apply_lesson, through the UNGUARDED apply loop in _run_with_error,
# and killed the reflection thread.

from hermes_bridge.memory import _slug  # noqa: E402


def test_slug_is_bounded_for_the_filesystem():
    long_name = "sustained-delta-sign-persistence " * 20      # ~660 chars
    s = _slug(long_name)
    assert len(s) <= 120, f"slug must be bounded, got {len(s)}"
    assert len(s) + len(".md.tmp") < 255


def test_slug_stays_unique_after_truncation():
    """Two long names sharing a prefix must not collide onto one file."""
    base = "a-very-long-lesson-name-that-goes-on-and-on " * 6
    assert _slug(base + "first-distinct-tail") != _slug(base + "second-distinct-tail")


def test_slug_is_stable_for_the_same_name():
    """update/retire must resolve to the same file as create."""
    n = "some-quite-long-lesson-name " * 10
    assert _slug(n) == _slug(n)


def test_apply_lesson_writes_a_very_long_name(tmp_path):
    ls = LearnedStore(str(tmp_path / "learned"))
    name = ("sustained delta sign persistence 10-16 same-sign bars single bar as low as "
            "0.023 0.038 may substitute for magnitude floor only on post-acceptance "
            "continuation hold confirm rungs in trending regime holding a reclaimed shelf "
            "never on first breaks counter-trend mixed-sign tape or zero-pullback "
            "staircase grinds making fresh highs lows still subject to location "
            "clearance veto")
    assert len(name) > 300
    ls.apply_lesson("create", name, body="BODY-MARKER")
    got = ls.lessons()
    assert len(got) == 1
    assert got[0].body.strip() == "BODY-MARKER"
    assert got[0].name == name          # the FULL name survives in frontmatter


def test_reflection_survives_an_unwritable_lesson(tmp_path, monkeypatch):
    """One bad lesson must not kill the reflection thread — the apply loop needs the same
    guard curate()/distill() got."""
    cfg, learned, r = _reflector(tmp_path)

    def _boom_apply(*a, **k):
        raise OSError("filename too long")

    monkeypatch.setattr(learned, "apply_lesson", _boom_apply)
    monkeypatch.setattr(
        "hermes_bridge.reflect.run_claude_oneshot",
        lambda *a, **k: '{"structured_output": {"lessons": [{"op": "create", "name": "x"}]}}',
    )
    out = r._run_with_error("sys", "user", "{}")   # must NOT raise
    assert out["lessons"] == 0
    assert out["error"]
