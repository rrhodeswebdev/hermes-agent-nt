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
