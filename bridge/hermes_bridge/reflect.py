"""Self-improvement — reflection (per closed trade) and curation (periodic).

Tool-less Claude calls: the model PROPOSES learned-knowledge updates as structured
output; the bridge validates and applies them via LearnedStore (which writes only
under learned_dir). The model never writes files, places orders, or changes risk.
Every failure is swallowed -- reflection is best-effort and must never disrupt trading.
"""

from __future__ import annotations

import json
from collections import Counter

from .claude_cli import extract_structured, run_claude_oneshot
from .config import BridgeConfig
from .journal import ClosedTrade, JournalStore
from .market_calendar import _et, _et_date
from .memory import LearnedStore


def _rth_window(now: float) -> tuple[float, float]:
    """(start_ts, end_ts) of the RTH session (09:30-16:00 ET) on now's ET date."""
    et = _et(now)
    base = et.replace(hour=0, minute=0, second=0, microsecond=0)
    start = base.replace(hour=9, minute=30)
    end = base.replace(hour=16, minute=0)
    return start.timestamp(), end.timestamp()


def build_day_digest(now: float, declines: list[dict], pa: dict,
                     trades: list[dict], session: dict) -> dict:
    """Deterministic RTH-day digest (no model call). Filters declines/trades to today's
    RTH window by resolved_ts/ts and rolls up the counts the day-review narrates."""
    lo, hi = _rth_window(now)

    def _in(d: dict, key: str) -> bool:
        t = d.get(key) or 0
        return lo <= t <= hi

    dec = [d for d in declines if _in(d, "resolved_ts")]
    trd = [t for t in trades if _in(t, "ts")]

    def _band(c) -> str:
        c = c or 0
        return "<0.30" if c < 0.30 else "0.30-0.50" if c < 0.50 else ">=0.50"

    items = [{"side": d.get("side"), "regime": d.get("regime"),
              "conf": d.get("confidence"), "delta": d.get("delta_ratio"),
              "outcome": d.get("outcome"), "supp": d.get("suppressed_by"),
              "why": str(d.get("rationale"))[:80]} for d in dec[:20]]
    return {
        "date": _et_date(now).isoformat(),
        "trades": {"count": len(trd),
                   "pnl": round(sum(t.get("realized_pnl") or 0 for t in trd), 2)},
        "declines": {
            "total": len(dec),
            "by_outcome": dict(Counter(d.get("outcome") for d in dec)),
            "by_suppressed": dict(Counter(str(d.get("suppressed_by") or "") for d in dec)),
            "by_regime": dict(Counter(d.get("regime") for d in dec)),
            "by_side": dict(Counter(d.get("side") for d in dec)),
            "conf_bands": dict(Counter(_band(d.get("confidence")) for d in dec)),
            "items": items,
        },
        "pa": pa,
        "session": session,
    }


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
have hit their target (counterfactual outcomes below). Check:
(1) Is a learned LESSON over-blocking? If the pattern is clear, narrow it per the rules.
(2) Is this a COVERAGE gap — would-win declines clustering as ONE setup type the playbook
never armed (e.g. trend-CONTINUATION entries the pre-session study left out while a trend
ran)? If so, add a concise notes_append naming that setup type + regime so the NEXT
pre-session study authors it (the authoring step reads these notes). That is how a missed
trend day becomes a learned setup instead of a repeated miss.
(3) Records with kind "early_exit" and outcome "would_win" mean a TAKEN trade was exited
but price then reached its original target — an exit too tight / a shakeout. If several
cluster, add a notes_append to give invalidations room beyond the noise and to discount
delta on abnormally light-volume bars.
(4) Each decline carries "suppressed_by" (the gate that blocked it: min_confidence |
transitional | delta_floor), plus "delta_ratio" and "confidence" at decline. If would-win
declines CLUSTER on ONE gate in ONE session (e.g. several delta_floor would-wins in ETH,
whose tape grinds rather than spikes), name it in a notes_append: session + gate + the
entry SHAPE that would pass (e.g. "in ETH grinds, favor continuation setups that confirm on
a SUSTAINED delta lean, not a single-bar spike"). TEXT ONLY — never propose a numeric
floor/config value; the operator calibrates those from the re-scored tape.
CRITICAL: would-win declines are NOT free money. Weigh them against the would-LOSE declines
in the SAME cluster — a gate that blocks a 25%-win chop cluster is doing its job, and
narrowing it would re-admit the losers. Only flag a gate when its would-wins clearly and
repeatedly DOMINATE its would-loses.
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

EOD_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "theme": {"type": "string"},
        "observation": {"type": "string"},
    },
    "required": ["narrative", "theme"],
})

EOD_SYSTEM = """\
You are reviewing one RTH trading day for a futures day-trading agent. You are given a \
DETERMINISTIC digest (price action, declined/unfilled setups with outcomes, trades). Write a \
concise, specific day-review of what the price action did and why the agent traded or didn't \
— name the regime, the entry-style fit, and the would-win/would-lose verdict. Do NOT propose \
config or risk numbers. Return JSON: narrative (the writeup), theme (a short snake_case \
pattern key for this day, e.g. trend_day_pullback_subconfidence), observation (one candidate \
insight). Be honest when sitting out was correct."""

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
        # Auto-consolidation cadence state (server._start_consolidation drives it).
        # _last_curate_ts starts at the current lessons mtime so curate fires only on
        # lessons that change AFTER startup; distill handles the startup catch-up.
        self._last_curate_ts: float = self.learned.lessons_mtime()
        self._last_check_ts: float | None = None
        self._last_summary: str | None = None

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

    def reflect_on_day(self, digest: dict) -> dict:
        """Descriptive once-a-day review over the deterministic digest. Best-effort: writes a
        dated day-review on success, swallows every failure (never disrupts trading)."""
        lc = self.cfg.learning
        out = {"written": 0, "theme": None, "error": None}
        user = "RTH DAY DIGEST:\n" + json.dumps(digest, separators=(",", ":"))
        try:
            reply = run_claude_oneshot(self.cfg.agent.claude, EOD_SYSTEM, user,
                                       json_schema=EOD_SCHEMA, model=lc.reflect_model,
                                       timeout_s=self.cfg.agent.claude.timeout_s)
            proposals = extract_structured(reply)
        except Exception as e:  # noqa: BLE001 — best-effort
            out["error"] = type(e).__name__
            return out
        if not isinstance(proposals, dict) or not str(proposals.get("narrative", "")).strip():
            out["error"] = "no_narrative"
            return out
        body = str(proposals["narrative"]).strip()
        theme = str(proposals.get("theme") or "").strip() or None
        obs = str(proposals.get("observation") or "").strip()
        if obs:
            body += f"\n\n_theme: {theme or '?'} · observation: {obs}_"
        elif theme:
            body += f"\n\n_theme: {theme}_"
        self.learned.append_day_review(digest.get("date", "?"), body, lc.day_review_keep)
        out["written"], out["theme"] = 1, theme
        return out

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

    def mark_alive(self, now: float) -> None:
        """Stamp the liveness heartbeat WITHOUT running a pass — called at the daemon
        thread's start so check_age_s is observable from t0 (a thread that dies during
        the startup grace still shows a growing age, which the monitor alarms on)."""
        self._last_check_ts = now

    def consolidate_once(self, now: float) -> dict:
        """One consolidation check. curate() when lessons changed since the last tidy;
        distill() when the corpus is newer than distilled.md. Material-gated — makes NO
        model call when nothing changed, only advances the heartbeat. Best-effort:
        curate()/distill() already swallow every exception, so this never disrupts trading."""
        ls = self.learned
        need_distill = ls.corpus_mtime() > ls.distilled_mtime()
        need_curate = ls.lessons_mtime() > self._last_curate_ts
        curated = distilled = 0
        if need_curate:
            self.curate()
            # Watermark to the ACTUAL post-curate lessons mtime (not `now`): curate writes
            # after `now` was captured, so keying off `now` could re-trigger every cycle.
            self._last_curate_ts = ls.lessons_mtime()
            curated = 1
        if need_curate or need_distill:
            applied = self.distill()
            distilled = 1 if applied.get("distilled") else 0
        self._last_check_ts = now
        if curated or distilled:
            self._last_summary = f"curated={curated} distilled={distilled}"
            return {"curated": curated, "distilled": distilled, "skipped": None}
        self._last_summary = "skip:no_new_material"
        return {"curated": 0, "distilled": 0, "skipped": "no_new_material"}

    def consolidation_status(self, now: float) -> dict:
        """Read-only freshness + liveness for the dashboard/panel. check_age_s is None
        only when the daemon never started (consolidation disabled)."""
        dm = self.learned.distilled_mtime()
        return {
            "enabled": self.cfg.learning.consolidate_enabled,
            "check_age_s": (now - self._last_check_ts) if self._last_check_ts else None,
            "distilled_age_s": (now - dm) if dm else None,
            "last": self._last_summary,
        }

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
