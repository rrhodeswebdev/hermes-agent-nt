"""Semantic memory — the learned knowledge injected into each decision prompt.

Reads three artifacts from `learned_dir` and formats them into one bounded block:
  * trader-profile.md  — who the trader is / their imposed rules (the user model)
  * agent-notes.md     — the agent's own observations about this instrument/regime
  * lessons/*.md       — distilled plays, each with YAML frontmatter (name, status, ...)

Writers (reflection/curation) keep a timestamped backup of every overwrite under
`.history/` — the live files are gitignored (per-trader), so this is their revert trail.
Profile changes from reflection are only ever written as PROPOSALS (see propose_profile).
Excludes lessons whose frontmatter `status` is not "active".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---\\n<yaml>\\n---\\n<body>`. Returns ({}, text) when no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].lstrip("\n")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s or "lesson"


def _split_bullets(text: str) -> tuple[str, list[str]]:
    """Split markdown into (header, bullets): a bullet starts at a line beginning with
    '- '; continuation lines stay with their bullet. Header = everything before the
    first bullet."""
    header: list[str] = []
    bullets: list[str] = []
    cur: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("- "):
            if cur is not None:
                bullets.append("\n".join(cur))
            cur = [line]
        elif cur is not None:
            cur.append(line)
        else:
            header.append(line)
    if cur is not None:
        bullets.append("\n".join(cur))
    return "\n".join(header).strip(), bullets


def truncate_at_boundary(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` chars without cutting mid-word or mid-bullet.

    Within limit -> unchanged. Otherwise cut at the last complete unit before the
    limit — a bullet/heading line boundary first, then a sentence end, then the
    last whitespace, then a hard cut — and append a truncation marker."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 2:
        return text[:limit]  # no room for a marker — the length contract wins
    head = text[: max(0, limit - 2)]  # reserve room for the "\n…" marker
    line_cut = max(head.rfind("\n- "), head.rfind("\n#"))
    sent_cut = head.rfind(". ")
    space_cut = max(head.rfind(" "), head.rfind("\n"))
    if line_cut > 0:
        cut = line_cut
    elif sent_cut > 0:
        cut = sent_cut + 1  # keep the period
    elif space_cut > 0:
        cut = space_cut
    else:
        cut = len(head)
    return head[:cut].rstrip() + "\n…"


def _render_lesson(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body.strip()}\n"


@dataclass(frozen=True)
class Lesson:
    name: str
    status: str
    body: str
    meta: dict


class LearnedStore:
    """The learned-knowledge directory: bounded prompt view + guarded writers."""

    _HISTORY_KEEP = 20  # backups kept per file; learned writes are low-rate

    def __init__(self, learned_dir: str) -> None:
        self.dir = Path(learned_dir)

    def _read(self, name: str) -> str:
        f = self.dir / name
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""

    def profile(self) -> str:
        return self._read("trader-profile.md")

    def notes(self) -> str:
        return self._read("agent-notes.md")

    def distilled(self) -> str:
        """The distilled lessons (written by Reflector.distill, a slower/deeper model
        pass). When present it REPLACES raw lessons in decision prompts, so the lesson
        corpus can grow without bloating the per-bar prompt."""
        return self._read("distilled.md")

    def set_distilled(self, text: str) -> None:
        self._atomic_write(self.dir / "distilled.md", text.strip() + "\n")

    def day_reviews_mtime(self) -> float:
        return self._mtime(self.dir / "day-reviews.md")

    def day_reviews(self, n: int) -> list[tuple[str, str]]:
        """Newest-first (date, body) pairs from day-reviews.md (## <date> sections)."""
        text = self._read("day-reviews.md")
        if not text.strip():
            return []
        out: list[tuple[str, str]] = []
        cur_date: str | None = None
        cur: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if cur_date is not None:
                    out.append((cur_date, "\n".join(cur).strip()))
                cur_date, cur = line[3:].strip(), []
            elif cur_date is not None:
                cur.append(line)
        if cur_date is not None:
            out.append((cur_date, "\n".join(cur).strip()))
        return out[:n]

    def append_day_review(self, date_str: str, body: str, keep: int) -> None:
        """Prepend a dated review, keep the newest `keep`. Atomic; tool-less text only."""
        existing = [(d, b) for d, b in self.day_reviews(10_000) if d != date_str]
        entries = [(date_str, body.strip())] + existing
        entries = entries[: max(1, keep)]
        rendered = "\n\n".join(f"## {d}\n{b}" for d, b in entries)
        self._atomic_write(self.dir / "day-reviews.md", rendered.strip() + "\n")

    @staticmethod
    def _mtime(path: Path) -> float:
        return path.stat().st_mtime if path.is_file() else 0.0

    def distilled_mtime(self) -> float:
        return self._mtime(self.dir / "distilled.md")

    def lessons_mtime(self) -> float:
        d = self.dir / "lessons"
        if not d.is_dir():
            return 0.0
        return max((f.stat().st_mtime for f in d.glob("*.md")), default=0.0)

    def corpus_mtime(self) -> float:
        """Newest mtime across the full distillation input: live notes, lessons,
        profile, and the long-term notes archive. 0.0 when none exist yet. (.history/
        backups and the *.proposed profile are deliberately excluded — not corpus.)"""
        return max(
            self._mtime(self.dir / "agent-notes.md"),
            self._mtime(self.dir / "trader-profile.md"),
            self._mtime(self.dir / "archive" / "agent-notes-archive.md"),
            self.lessons_mtime(),
        )

    def archived_notes(self, tail_chars: int = 3000) -> str:
        """The newest slice of long-term archived notes (distillation input)."""
        f = self.dir / "archive" / "agent-notes-archive.md"
        if not f.is_file():
            return ""
        return f.read_text(encoding="utf-8")[-tail_chars:]

    def lessons(self) -> list[Lesson]:
        d = self.dir / "lessons"
        if not d.is_dir():
            return []
        out: list[Lesson] = []
        # Most recently created/updated first: when the prompt budget can't hold every
        # lesson, the freshest learning survives (alphabetical-slug order made the cut
        # arbitrary). Curation is the real fix for an over-budget lesson set.
        files = sorted(d.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            status = str(meta.get("status", "active"))
            if status != "active":
                continue
            out.append(Lesson(name=str(meta.get("name", f.stem)), status=status,
                              body=body.strip(), meta=meta))
        return out

    def format_for_prompt(self, profile_chars: int = 1400, notes_chars: int = 2200,
                          lessons_chars: int = 2500, day_reviews_n: int = 0,
                          day_reviews_chars: int = 1800) -> str:
        sections: list[str] = []
        notes_dropped = lessons_dropped = 0
        p = self.profile()
        if p:
            sections.append("=== TRADER PROFILE ===\n" + p[:profile_chars])
        n = self.notes()
        if n:
            shown, notes_dropped = self._notes_for_prompt(n, notes_chars)
            sections.append("=== AGENT NOTES ===\n" + shown)
        distilled = self.distilled()
        if distilled:
            # Distilled tier active: it stands in for the raw lessons (fresh notes
            # above still flow until the next distillation pass).
            sections.append("=== DISTILLED LESSONS ===\n" + distilled[:lessons_chars])
            lessons = []
        else:
            lessons = self.lessons()
        if lessons:
            lines, used = [], 0
            for ls in lessons:
                entry = f"- [{ls.name}] {ls.body}"
                if used + len(entry) > lessons_chars:
                    lessons_dropped += 1
                    continue
                lines.append(entry)
                used += len(entry)
            if lessons_dropped:
                lines.append(f"(… {lessons_dropped} more lessons over the prompt budget — "
                             f"run curation to consolidate)")
            if lines:
                sections.append("=== LEARNED LESSONS ===\n" + "\n".join(lines))
        if day_reviews_n > 0:
            revs = self.day_reviews(day_reviews_n)
            if revs:
                block, used = [], 0
                for d, b in revs:
                    entry = f"[{d}] {b}"
                    if used + len(entry) > day_reviews_chars:
                        break
                    block.append(entry)
                    used += len(entry)
                if block:
                    sections.append("=== RECENT DAY-REVIEWS ===\n" + "\n\n".join(block))
        # Truncation was silent before; tell the operator once per change, not per call.
        report = (notes_dropped, lessons_dropped)
        if any(report) and report != getattr(self, "_last_trunc_report", None):
            print(f"[learned] prompt budget truncation: notes_dropped={notes_dropped} "
                  f"lessons_dropped={lessons_dropped} (archive/curation holds the rest)",
                  flush=True)
        self._last_trunc_report = report
        return "\n\n".join(sections)

    @staticmethod
    def _notes_for_prompt(text: str, budget: int) -> tuple[str, int]:
        """NEWEST notes first under the budget. (The old head-truncate kept the OLDEST
        chars, silently cutting every new insight once the file grew past the limit.)
        Returns (rendered, dropped_bullet_count)."""
        if len(text) <= budget:
            return text, 0
        header, bullets = _split_bullets(text)
        if not bullets:
            return text[:budget], 0
        # Reserve room for the omission banner up front (we reach here only when the text
        # already exceeds budget, so something WILL be dropped), then size newest-first by the
        # ACTUAL rendered length — banner + the "\n" join inserts — so the result never exceeds
        # the budget (a truncated bullet counts only the chars kept, not its full length).
        banner = "(… {n} older notes omitted — full history in the archive)"
        reserve = len(banner.format(n=len(bullets))) + 1  # + the newline after the banner
        inner = max(0, budget - reserve)
        picked: list[str] = []
        used = 0
        for b in reversed(bullets):
            sep = 1 if picked else 0  # the "\n" join() inserts between bullets
            if used + sep + len(b) <= inner:
                picked.append(b)
                used += sep + len(b)
            else:
                remain = inner - used - sep
                if remain > 0:
                    picked.append(b[:remain])
                break
        dropped = len(bullets) - len(picked)
        out: list[str] = []
        if dropped > 0:
            out.append(banner.format(n=dropped))
        out.extend(reversed(picked))  # chronological order among the selected
        return "\n".join(out)[:budget], dropped

    def _backup(self, path: Path) -> None:
        """Timestamped copy under .history/ before any overwrite. The live files are
        gitignored (per-trader), so this — not git — is the revert trail for every
        write the reflection loop makes."""
        if not path.is_file():
            return
        hist = self.dir / ".history"
        hist.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        (hist / f"{path.stem}.{stamp}{path.suffix}").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
        for old in sorted(hist.glob(f"{path.stem}.*{path.suffix}"))[:-self._HISTORY_KEEP]:
            old.unlink()

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._backup(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def set_profile(self, text: str) -> None:
        self._atomic_write(self.dir / "trader-profile.md", text.strip() + "\n")

    def propose_profile(self, text: str) -> None:
        """Reflection may only PROPOSE profile changes. trader-profile.md is user-authored
        and feeds every future decision/reflection prompt — auto-replacing it would let one
        bad reflection self-amplify. A human merges (or deletes) the proposal."""
        self._atomic_write(self.dir / "trader-profile.proposed.md", text.strip() + "\n")

    def append_note(self, note: str, *, archive_over_chars: int = 8000,
                    keep_chars: int = 4000) -> None:
        note = note.strip()
        if not note:
            return
        existing = self.notes()
        body = (existing.rstrip() + "\n- " + note) if existing else "# Agent Notes\n\n- " + note
        body = self._archive_overflow(body, archive_over_chars, keep_chars)
        self._atomic_write(self.dir / "agent-notes.md", body + "\n")

    def _archive_overflow(self, body: str, archive_over: int, keep: int) -> str:
        """Once the live notes file outgrows archive_over, move the OLDEST bullets to
        archive/agent-notes-archive.md, keeping the newest ~keep chars as the working
        set. Long-term memory is never deleted — the archive holds everything that
        leaves the working set (and .history/ has the pre-write backups)."""
        if archive_over <= 0 or len(body) <= archive_over:
            return body
        keep = max(1, min(keep, archive_over))
        header, bullets = _split_bullets(body)
        if len(bullets) < 2:
            return body
        kept: list[str] = []
        used = 0
        for b in reversed(bullets):
            if used + len(b) > keep and kept:
                break
            kept.append(b)
            used += len(b)
        kept.reverse()
        to_archive = bullets[: len(bullets) - len(kept)]
        if not to_archive:
            return body
        arch = self.dir / "archive" / "agent-notes-archive.md"
        arch.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        with arch.open("a", encoding="utf-8") as f:
            f.write(f"\n## archived {stamp}\n" + "\n".join(to_archive) + "\n")
        return (header or "# Agent Notes") + "\n\n" + "\n".join(kept)

    def apply_lesson(self, op: str, name: str, body: str = "",
                     regime_tags: list[str] | None = None) -> None:
        path = self.dir / "lessons" / f"{_slug(name)}.md"
        if op == "retire":
            if path.is_file():
                meta, b = parse_frontmatter(path.read_text(encoding="utf-8"))
                meta["status"] = "retired"
                self._atomic_write(path, _render_lesson(meta, b))
            return
        meta: dict = {}
        if path.is_file() and op == "update":
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        meta["name"] = name
        meta.setdefault("status", "active")
        if regime_tags is not None:
            meta["regime_tags"] = list(regime_tags)
        self._atomic_write(path, _render_lesson(meta, body))
