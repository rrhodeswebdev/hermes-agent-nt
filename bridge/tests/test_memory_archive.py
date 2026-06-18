"""Notes triage/archival: newest-first prompt view, deterministic archival of the
oldest bullets to long-term storage, and recency-ordered lessons under the budget.
The old behavior kept the OLDEST 2200 chars in the prompt — every new insight was
silently cut once the file grew past the limit."""

import os
import time

from hermes_bridge.memory import LearnedStore, _split_bullets


def test_split_bullets_keeps_continuation_lines():
    header, bullets = _split_bullets("# Agent Notes\n\n- first note\n  spans lines\n- second")
    assert header == "# Agent Notes"
    assert bullets == ["- first note\n  spans lines", "- second"]


def test_notes_prompt_keeps_newest(tmp_path):
    s = LearnedStore(str(tmp_path))
    for i in range(12):
        s.append_note(f"note {i:02d} " + "x" * 60)
    out = s.format_for_prompt(notes_chars=200)
    assert "note 11" in out                       # newest survives
    assert "note 00" not in out                   # oldest dropped from the prompt
    assert "older notes omitted" in out
    assert "note 00" in s.notes()                 # live file untouched by the view


def test_notes_under_budget_unchanged(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.append_note("only note")
    out = s.format_for_prompt()
    assert "only note" in out
    assert "omitted" not in out


def test_append_note_archives_oldest_without_losing_anything(tmp_path):
    s = LearnedStore(str(tmp_path))
    for i in range(10):
        s.append_note(f"obs {i:02d} " + "y" * 50, archive_over_chars=300, keep_chars=150)
    live = s.notes()
    arch = (tmp_path / "archive" / "agent-notes-archive.md").read_text(encoding="utf-8")
    assert "obs 09" in live                       # newest stays in the working set
    assert "obs 00" in arch                       # oldest moved to long-term memory
    for i in range(10):                           # preserved EXACTLY once — never
        assert (live + arch).count(f"obs {i:02d}") == 1  # deleted, never duplicated
    assert len(live) < 300 + 80                   # live file stays bounded (+1 new note)


def test_archived_notes_reads_tail(tmp_path):
    s = LearnedStore(str(tmp_path))
    for i in range(10):
        s.append_note(f"obs {i:02d} " + "y" * 50, archive_over_chars=300, keep_chars=150)
    assert "obs 00" in s.archived_notes()         # the distillation input sees archived bullets
    assert s.archived_notes(tail_chars=5) != ""   # honors a small tail window


def test_archive_off_when_threshold_zero(tmp_path):
    s = LearnedStore(str(tmp_path))
    for i in range(10):
        s.append_note(f"obs {i:02d} " + "y" * 50, archive_over_chars=0, keep_chars=150)
    assert not (tmp_path / "archive" / "agent-notes-archive.md").is_file()
    for i in range(10):                           # everything stays in the live file
        assert f"obs {i:02d}" in s.notes()


def test_lessons_budget_keeps_most_recently_updated(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.apply_lesson("create", "old-lesson", body="A" * 120)
    s.apply_lesson("create", "new-lesson", body="B" * 120)
    old_f = tmp_path / "lessons" / "old-lesson.md"
    now = time.time()
    os.utime(old_f, (now - 3600, now - 3600))     # make 'old-lesson' clearly older
    out = s.format_for_prompt(lessons_chars=150)  # budget fits only one lesson
    assert "new-lesson" in out
    assert "old-lesson" not in out
    assert "more lessons over the prompt budget" in out
