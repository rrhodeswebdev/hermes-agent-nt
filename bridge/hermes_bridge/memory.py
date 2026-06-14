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

    def lessons(self) -> list[Lesson]:
        d = self.dir / "lessons"
        if not d.is_dir():
            return []
        out: list[Lesson] = []
        for f in sorted(d.glob("*.md")):
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            status = str(meta.get("status", "active"))
            if status != "active":
                continue
            out.append(Lesson(name=str(meta.get("name", f.stem)), status=status,
                              body=body.strip(), meta=meta))
        return out

    def format_for_prompt(self, profile_chars: int = 1400, notes_chars: int = 2200,
                          lessons_chars: int = 2500) -> str:
        sections: list[str] = []
        p = self.profile()
        if p:
            sections.append("=== TRADER PROFILE ===\n" + p[:profile_chars])
        n = self.notes()
        if n:
            sections.append("=== AGENT NOTES ===\n" + n[:notes_chars])
        lessons = self.lessons()
        if lessons:
            lines, used = [], 0
            for ls in lessons:
                entry = f"- [{ls.name}] {ls.body}"
                if used + len(entry) > lessons_chars:
                    break
                lines.append(entry)
                used += len(entry)
            if lines:
                sections.append("=== LEARNED LESSONS ===\n" + "\n".join(lines))
        return "\n\n".join(sections)

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

    def append_note(self, note: str) -> None:
        note = note.strip()
        if not note:
            return
        existing = self.notes()
        body = (existing.rstrip() + "\n- " + note) if existing else "# Agent Notes\n\n- " + note
        self._atomic_write(self.dir / "agent-notes.md", body + "\n")

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
