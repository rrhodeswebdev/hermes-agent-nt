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
- DECLINED-SETUP counterfactuals may be provided: entries the agent vetoed (or plans
  that never filled), replayed against later real bars with the strategy's ATR brackets.
  If declines citing a specific lesson consistently show "would_win", that lesson may be
  OVER-BLOCKING: prefer op "update" that NARROWS its scope to the regime/condition where
  it actually holds (e.g. "applies in range-bound tape; in a strong breakout trend with
  positive delta, require only 0.5×ATR clearance") over retiring it — narrow or relax,
  don't just pile on another restriction. If they show "would_lose", the lesson is earning
  its keep — change nothing. Counterfactuals are approximations (no slippage, no queue
  position): require a clear repeated pattern (3+ similar outcomes) before weakening any
  lesson, and count near-duplicate declines of the SAME move (similar price/time) as ONE.
- Keep each lesson body to 1-3 sentences."""

MISSED_HEADER = """\
NO TRADE CLOSED — this reflection was triggered because several DECLINED setups would
have hit their target (counterfactual outcomes below). Check two things:
(1) Is a learned LESSON over-blocking? If the pattern is clear, narrow it per the rules.
(2) Is this a COVERAGE gap — would-win declines clustering as ONE setup type the playbook
never armed (e.g. trend-CONTINUATION entries the pre-session study left out while a trend
ran)? If so, add a concise notes_append naming that setup type + regime so the NEXT
pre-session study authors it (the authoring step reads these notes). That is how a missed
trend day becomes a learned setup instead of a repeated miss.
Near-duplicate declines of the SAME move (similar price/time) count as ONE pattern, not
several. If the declines look unrelated and thin, return an empty "lessons" array."""

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

    def reflect_on_missed(self, declines: list[dict], recent: list[dict]) -> dict:
        """Reflection with no closed trade: would-win declines accumulated (the
        over-blocking signal closed trades can never carry). Returns an applied dict
        whose "error" makes a silent failure visible (None = the call ran and its
        proposals were applied; a string = nothing was learned and why)."""
        user = (
            MISSED_HEADER
            + self._declines_block(declines)
            + "\n\nRECENT TRADES (most recent last):\n"
            + json.dumps(recent[-self.cfg.learning.reflect_recent:], separators=(",", ":"))
        )
        return self._run_with_error(self._system(REFLECT_SYSTEM), user, REFLECT_SCHEMA)

    @staticmethod
    def _declines_block(declines: list[dict] | None) -> str:
        if not declines:
            return ""
        return ("\n\nDECLINED/UNFILLED SETUPS — counterfactual outcomes (approximate):\n"
                + json.dumps(declines, separators=(",", ":")))

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
        if not isinstance(proposals, dict):
            proposals = {}  # extract_structured is dict|None; never crash this fail-safe path
        text = proposals.get("distilled")
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
            proposals = extract_structured(reply)
            if not isinstance(proposals, dict):
                proposals = {}
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

    def _run_with_error(self, system: str, user: str, schema: str) -> dict:
        # Like _run, but carries an "error" key so a silent failure is visible to
        # callers/logs (the flat-only missed-reflection has no closed-trade side effect
        # to notice it failed): None = the call ran and its (possibly empty) proposals
        # were applied; a string = nothing was learned and why (CLI timeout vs
        # unparseable reply look identical otherwise). Mirrors distill()'s style.
        applied = {"lessons": 0, "notes": 0, "profile": 0, "error": None}
        try:
            reply = run_claude_oneshot(self.cfg.agent.claude, system, user,
                                       json_schema=schema, model=self.cfg.learning.reflect_model)
            proposals = extract_structured(reply)
        except Exception as e:  # noqa: BLE001 — best-effort; never disrupt trading
            applied["error"] = type(e).__name__
            return applied
        if not isinstance(proposals, dict):
            applied["error"] = "no_structured_output"
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
            self.learned.propose_profile(str(pr))
            applied["profile"] = 1
        return applied
