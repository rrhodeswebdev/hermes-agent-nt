"""Self-improvement — reflection (per closed trade) and curation (periodic).

Tool-less Claude calls: the model PROPOSES learned-knowledge updates as structured
output; the bridge validates and applies them via LearnedStore (which writes only
under learned_dir). The model never writes files, places orders, or changes risk.
Every failure is swallowed -- reflection is best-effort and must never disrupt trading.
"""

from __future__ import annotations

import json

from .claude_cli import extract_structured, run_claude_oneshot
from .config import BridgeConfig
from .journal import ClosedTrade, JournalStore
from .memory import LearnedStore

_LESSON_PROPS = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["create", "update", "retire"]},
        "name": {"type": "string"},
        "regime_tags": {"type": "array", "items": {"type": "string"}},
        "body": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["op", "name"],
}

REFLECT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": _LESSON_PROPS},
        "notes_append": {"type": "array", "items": {"type": "string"}},
        "profile_replace": {"type": ["string", "null"]},
    },
    "required": ["lessons"],
}, separators=(",", ":"))

REFLECT_SYSTEM = """\
You are the trading agent's reflection module. You are given a just-closed trade,
recent trades, and the agent's current learned knowledge. Propose updates that make
FUTURE decisions better. Return ONLY structured output per the schema.

Rules:
- Be conservative. Most single trades teach nothing -- return an empty "lessons" array
  unless there is a clear, GENERALIZABLE pattern (ideally seen across several trades).
- Prefer UPDATING an existing lesson over creating a near-duplicate. Use op "retire"
  only when outcomes consistently contradict an existing lesson.
- Capture only durable, strategy-level plays (e.g. "counter-trend entries in the first
  30 minutes underperform"). Do NOT capture one-off narratives, tooling/environment
  issues, or restatements of the static strategy.
- notes_append: short factual observations about this instrument/regime worth keeping.
- profile_replace: set only if the trader's stated preferences/risk posture changed;
  otherwise null. (It is saved as a PROPOSAL for the trader to review, never auto-applied.)
- Keep each lesson body to 1-3 sentences."""

CURATE_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"lessons": {"type": "array", "items": _LESSON_PROPS}},
    "required": ["lessons"],
}, separators=(",", ":"))

CURATE_SYSTEM = """\
You are the trading agent's lesson curator. You are given all current lessons with
their stats. Consolidate overlapping lessons (update one, retire the duplicates) and
retire lessons that are stale or contradicted. Return ONLY structured output: a
"lessons" array of op/name/body/regime_tags changes. Make NO change you are unsure of."""

DISTILL_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"distilled": {"type": "string"}},
    "required": ["distilled"],
}, separators=(",", ":"))

DISTILL_SYSTEM = """\
You are the trading agent's lesson DISTILLER — the slow, deep pass of a staggered
memory system. You are given the FULL learned corpus: trader profile, every active
lesson, live agent notes, and archived notes. Produce ONE compact distilled artifact
that the realtime decision agent will read INSTEAD of the raw lessons.

Rules:
- Hard rules first (the non-negotiables), then conditional heuristics WITH their
  regime/session conditions, then active watch-items (patterns still gathering data,
  with their current evidence counts).
- Preserve every load-bearing threshold EXACTLY as written (e.g. 1xATR clearance).
  Do not invent rules, soften rules, or resolve open questions — only compress,
  merge, and structure what exists. When notes conflict, keep the newer reading and
  fold the older one into a watch-item.
- Stay under {limit} characters. Plain markdown bullets. Text only — never numeric
  config or risk values as instructions to change."""


class Reflector:
    def __init__(self, cfg: BridgeConfig, learned: LearnedStore, journal: JournalStore) -> None:
        self.cfg = cfg
        self.learned = learned
        self.journal = journal

    def reflect_on_close(self, trade: ClosedTrade, recent: list[dict]) -> dict:
        user = (
            "JUST-CLOSED TRADE:\n" + json.dumps(trade.to_record(), separators=(",", ":"))
            + "\n\nRECENT TRADES (most recent last):\n"
            + json.dumps(recent[-self.cfg.learning.reflect_recent:], separators=(",", ":"))
        )
        return self._run(self._system(REFLECT_SYSTEM), user, REFLECT_SCHEMA)

    def distill(self) -> dict:
        """Slow-tier compression: the FULL corpus (profile, all lessons, live +
        archived notes) -> one bounded distilled.md via the deeper distill_model. The
        realtime prompt then reads the distilled text instead of raw lessons, capping
        per-bar prompt size no matter how much the agent learns.

        Tool-less + text-only: like reflection, the model PROPOSES text the bridge writes
        only under learned_dir via LearnedStore. It NEVER writes risk/config numbers,
        places orders, or changes any setting (the DISTILL_SYSTEM guard forbids numeric
        config as an instruction). Best-effort — every failure is swallowed."""
        lc = self.cfg.learning
        applied = {"distilled": 0, "error": None}
        lessons = "\n".join(f"- [{ls.name}] {ls.body}" for ls in self.learned.lessons())
        user = (
            "TRADER PROFILE:\n" + (self.learned.profile() or "(none)")
            + "\n\nACTIVE LESSONS (full):\n" + (lessons or "(none)")
            + "\n\nAGENT NOTES (live):\n" + (self.learned.notes() or "(none)")
            + "\n\nARCHIVED NOTES (older):\n" + (self.learned.archived_notes() or "(none)")
        )
        system = DISTILL_SYSTEM.replace("{limit}", str(lc.distilled_char_limit))
        try:
            reply = run_claude_oneshot(self.cfg.agent.claude, system, user,
                                       json_schema=DISTILL_SCHEMA, model=lc.distill_model,
                                       timeout_s=lc.distill_timeout_s)
            proposals = extract_structured(reply)
        except Exception as e:  # noqa: BLE001 — best-effort; never disrupt trading
            applied["error"] = type(e).__name__
            return applied
        text = (proposals or {}).get("distilled")
        if not text or not str(text).strip():
            applied["error"] = "no_distilled"
            return applied
        # Hard cap AT the configured limit (what's written is exactly what prompts
        # show — an over-limit tail would be silently cut at display time otherwise).
        # Atomic write keeps a .history/ backup; revert by restoring or deleting
        # hermes/learned/distilled.md (raw lessons take over again).
        self.learned.set_distilled(str(text)[: lc.distilled_char_limit])
        applied["distilled"] = 1
        return applied

    def curate(self) -> dict:
        lessons = [{"name": ls.name, "regime_tags": ls.meta.get("regime_tags", []),
                    "body": ls.body} for ls in self.learned.lessons()]
        if not lessons:
            return {"lessons": 0, "notes": 0, "profile": 0}
        user = "CURRENT LESSONS:\n" + json.dumps(lessons, separators=(",", ":"))
        return self._run(CURATE_SYSTEM, user, CURATE_SCHEMA)

    def _system(self, header: str) -> str:
        learned = self.learned.format_for_prompt(
            self.cfg.learning.profile_char_limit, self.cfg.learning.notes_char_limit,
            self.cfg.learning.lessons_char_limit)
        return f"{header}\n\n=== CURRENT LEARNED KNOWLEDGE ===\n{learned}" if learned else header

    def _run(self, system: str, user: str, schema: str) -> dict:
        applied = {"lessons": 0, "notes": 0, "profile": 0}
        try:
            reply = run_claude_oneshot(self.cfg.agent.claude, system, user,
                                       json_schema=schema, model=self.cfg.learning.reflect_model)
            proposals = extract_structured(reply) or {}
        except Exception:  # noqa: BLE001 — reflection is best-effort; never disrupt trading
            return applied
        for ls in (proposals.get("lessons") or [])[: self.cfg.learning.max_lessons]:
            op, name = ls.get("op"), ls.get("name")
            if op in ("create", "update", "retire") and name:
                self.learned.apply_lesson(op, name, body=ls.get("body", "") or "",
                                          regime_tags=ls.get("regime_tags"))
                applied["lessons"] += 1
        lc = self.cfg.learning
        for note in (proposals.get("notes_append") or []):
            if note:
                self.learned.append_note(
                    str(note), archive_over_chars=lc.notes_archive_over_chars,
                    keep_chars=lc.notes_keep_chars)
                applied["notes"] += 1
        pr = proposals.get("profile_replace")
        if pr:
            # Never auto-applied: the live profile is user-authored and self-amplifying
            # (it feeds every future prompt). Written as a proposal for human review.
            self.learned.propose_profile(str(pr))
            applied["profile"] = 1
        return applied
