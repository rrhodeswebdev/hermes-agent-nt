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
