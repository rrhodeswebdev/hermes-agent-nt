"""Claude agent client — the decision brain via the `claude` CLI on your subscription.

Talks to Claude Code in headless print mode (`claude -p --safe-mode`): it authenticates
off your Claude subscription (no ANTHROPIC_API_KEY / metered API) and runs isolated from
your global CLAUDE.md, hooks, MCP, and skills. The trading knowledge lives in the
`context/*.md` files; this client frames the request, runs one call, and parses the
response. Any failure degrades to WAIT / no-plan (never auto-trades on a malformed or
absent response — open positions remain protected by the resting bracket in NinjaTrader).

Three calls exist: `decide` (legacy per-bar Decision), `propose_plan` (between-bars
analysis arming a TradePlan for the next close), and `analyze_session` (one-time history
study producing the session brief, optionally on a bigger `session_model`).
"""

from __future__ import annotations

import json
import threading

from .agent_client import (
    DECISION_INSTRUCTION,
    AgentClient,
    AgentRequest,
    build_user_prompt,
    load_context_files,
)
from .claude_cli import ClaudeSession, extract_structured, run_claude_oneshot
from .config import BridgeConfig
from .journal import JournalStore, select_similar
from .memory import LearnedStore
from .models import Action, Bar, BrainTimeout, Decision
from .plan import PlanRequest, TradePlan

# JSON Schema for `--json-schema`: the Decision shape the agent must return.
DECISION_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            # FLATTEN is bridge-initiated (kill switch / goal hit), never an agent choice.
            "action": {"type": "string",
                       "enum": [a.value for a in Action if a is not Action.FLATTEN]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "qty": {"type": "integer", "minimum": 0},
            "stop_ticks": {"type": ["integer", "null"]},
            "target_ticks": {"type": ["integer", "null"]},
            "rationale": {"type": "string"},
        },
        "required": ["action"],
    },
    separators=(",", ":"),
)

# JSON Schema for `--json-schema`: the TradePlan shape a plan analysis must return.
# `mode` and `based_on_bar_ts` are absent on purpose — the bridge stamps them and
# never trusts the LLM for either (plan.py).
_TRIGGER_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["long", "short"]},
        "min_close": {"type": ["number", "null"]},
        "max_close": {"type": ["number", "null"]},
        "qty": {"type": "integer", "minimum": 1},  # an entry buys >=1 contract
        "stop_ticks": {"type": ["integer", "null"]},
        "target_ticks": {"type": ["integer", "null"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "rationale": {"type": "string"},
    },
    "required": ["direction"],
}
PLAN_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "bias": {"type": "string", "enum": ["long", "short", "neutral"]},
            "triggers": {"type": "array", "items": _TRIGGER_SCHEMA},
            "exit": {
                "type": ["object", "null"],
                "properties": {
                    "exit_below": {"type": ["number", "null"]},
                    "exit_above": {"type": ["number", "null"]},
                    "rationale": {"type": "string"},
                },
            },
            "rationale": {"type": "string"},
        },
        "required": ["triggers"],
    },
    separators=(",", ":"),
)

PLAN_INSTRUCTION = """\
=== YOUR TASK: ARM A PLAN FOR THE NEXT BAR CLOSE ===
You are running BETWEEN bars. Using the knowledge above (classify the regime first,
then apply the matching playbook), study the state below and arm explicit, mechanical
conditions for the NEXT bar close. The bridge will compare that close against your
conditions and act instantly — you will not be consulted at decision time.

Reply with one JSON object:
- "triggers": entry conditions (seek_entry plans). Each fires when
  min_close <= close <= max_close (omitted bound = unbounded; at least one bound
  required or the trigger never fires). Include the full bracket (stop_ticks /
  target_ticks), qty, and confidence per trigger. An empty list = no-trade plan.
- "exit": invalidation thresholds (manage_position plans): exit if the close is at/
  beyond exit_below or exit_above. null = hold, the resting bracket protects.
- "bias" and "rationale": your read, for the dashboard.

Arm a trigger only for a clean playbook setup; a no-trade plan is the correct output
for most bars. Hard risk limits are re-checked by the bridge on every fire.
"""

SESSION_INSTRUCTION = """\
=== YOUR TASK: PRE-SESSION STUDY ===
Study the historical bars below ONCE before the session. Using the knowledge above,
write a compact brief (~10-20 lines of plain text, no JSON) that your faster
per-bar plan analyses will rely on:
- the regime (trending/ranging/transitional) and the evidence for it,
- the key price levels that have mattered (with prices),
- volatility character vs ATR, time-of-day effects if visible,
- which playbook(s) apply and what would invalidate that read.
Be concrete and quantitative; every line must be usable without re-reading history.
"""


def build_plan_prompt(preq: PlanRequest) -> str:
    """Frame a plan-analysis request: brief + cycle context + current market state."""
    cycle = {
        "plan_for_mode": preq.mode,
        "assumed_position": preq.assumed_position,
        "based_on_bar_ts": preq.bar_ts,
        "outcome_at_close": preq.outcome,
        "levels": [lv.model_dump() for lv in preq.levels],
        "prior_plan": preq.prior_plan.model_dump() if preq.prior_plan else None,
    }
    return (
        "SESSION BRIEF (from your pre-session study):\n"
        + (preq.session_brief or "(none)")
        + "\n\nPLAN CYCLE CONTEXT:\n" + json.dumps(cycle, separators=(",", ":"))
        + "\n\n" + build_user_prompt(preq)
    )


_SESSION_HISTORY_BARS = 240  # ~one RTH day of 1m bars; keeps the study prompt bounded


def build_session_prompt(preq: PlanRequest, history: list[Bar]) -> str:
    """Frame the one-time history study (compact OHLCV, oldest first)."""
    bars = [
        {"ts": b.ts, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
        for b in history[-_SESSION_HISTORY_BARS:]
    ]
    payload = {
        "instrument": preq.account.instrument,
        "timeframe": preq.account.timeframe,
        "context": preq.context.to_dict(),
        "levels": [lv.model_dump() for lv in preq.levels],
        "bars": bars,
    }
    return "HISTORICAL DATA (study before the session):\n" + json.dumps(
        payload, separators=(",", ":"))


class ClaudeAgentClient(AgentClient):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._knowledge: str | None = None  # cached context files (rarely change)
        # Persistent sessions keyed by schema (≈ call kind): one for decide() and one
        # for propose_plan(); analyze_session stays one-shot (it may run on a different
        # model). The system prompt is deliberately NOT in the key — it embeds the
        # learned-memory block that reflection rewrites mid-session, so a (system, schema)
        # key would orphan a live child on every change. _session_ask recycles the child
        # in place when the prompt changes instead.
        self._sessions: dict[str, ClaudeSession] = {}
        # Guards the dict's get/recycle/spawn — NOT held during ask() (that would
        # serialize every decision). Only matters when decide() runs on FastAPI's
        # threadpool (planner disabled + persistent); the planner worker is single-thread.
        self._sessions_lock = threading.Lock()
        # Learned knowledge (trader profile, notes, lessons) and the closed-trade
        # journal are read back into prompts so reflection actually feeds the brain.
        self._learned = LearnedStore(config.learning.learned_dir)
        self._journal = JournalStore(config.learning.journal_path)

    def describe(self) -> str:
        return self.cfg.agent.claude.model

    def decide(self, req: AgentRequest) -> Decision:
        try:
            reply = self._ask(self._system_prompt(DECISION_INSTRUCTION),
                              self._user_message(req), DECISION_JSON_SCHEMA)
            return self._parse(reply)
        except Exception as exc:  # noqa: BLE001 — fail safe: never auto-trade on error
            return Decision(action=Action.WAIT, rationale=f"claude_error:{type(exc).__name__}")

    def _user_message(self, req: AgentRequest) -> str:
        """Market state, plus the most similar past trades from the journal so the
        brain reasons against its own recorded history (no-op when learning is off)."""
        user = build_user_prompt(req)
        lc = self.cfg.learning
        if lc.enabled and lc.retrieve_k > 0:
            similar = select_similar(self._journal.recent(200), req.context, lc.retrieve_k)
            if similar:
                user += ("\n\nRELEVANT PAST TRADES (same regime, most recent last):\n"
                         + json.dumps(similar, separators=(",", ":")))
        return user

    # ---- pre-armed plan cycle -------------------------------------------------
    def propose_plan(self, preq: PlanRequest) -> TradePlan | None:
        """Between-bars analysis. Parse failures return None (the Planner reports the
        error; a previously armed plan stays live until staleness retires it);
        transport errors (incl. BrainTimeout) propagate for the Planner to report."""
        reply = self._ask(self._system_prompt(PLAN_INSTRUCTION), build_plan_prompt(preq),
                          PLAN_JSON_SCHEMA, timeout_s=self.cfg.planner.plan_timeout_s)
        data = extract_structured(reply)
        if data is None or "triggers" not in data:
            # JSON without the schema-required "triggers" key is scraped garbage, not
            # a plan: every TradePlan field has a default, so it would validate into
            # an all-defaults plan, arm, and could replace a real exit rule with
            # "hold (bracket only)".
            return None
        try:
            return TradePlan.model_validate(data)
        except Exception:  # noqa: BLE001 — malformed plan = no plan, never a crash
            return None

    def analyze_session(self, preq: PlanRequest, history: list[Bar]) -> str:
        """One-time history study on `session_model` (falls back to `model`).
        Free-text reply — no schema; the brief is prose for the plan prompts."""
        c = self.cfg.agent.claude
        reply = run_claude_oneshot(
            c, self._system_prompt(SESSION_INSTRUCTION),
            build_session_prompt(preq, history),
            model=c.session_model or c.model,
            timeout_s=self.cfg.planner.session_timeout_s,
        )
        try:
            env = json.loads(reply)
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(env, dict) and not env.get("is_error"):
            res = env.get("result")
            return res if isinstance(res, str) else ""
        return ""

    # ---- plumbing ---------------------------------------------------------------
    def _system_prompt(self, instruction: str) -> str:
        # Rebuilt per call (cheaply): the learned block changes as reflection curates
        # lessons, so it must not be frozen in a cache. The static knowledge stays cached.
        if self._knowledge is None:
            c = self.cfg.agent.claude
            self._knowledge = load_context_files(c.context_dir) or c.context_hint
        parts = [self._knowledge]
        learned = self._learned_block()
        if learned:
            parts.append(learned)
        parts.append(instruction)
        return "\n\n".join(parts)

    def _learned_block(self) -> str:
        lc = self.cfg.learning
        if not lc.enabled:
            return ""
        return self._learned.format_for_prompt(
            lc.profile_char_limit, lc.notes_char_limit, lc.lessons_char_limit)

    def _ask(self, system: str, user: str, json_schema: str,
             timeout_s: float | None = None) -> str:
        c = self.cfg.agent.claude
        if c.persistent:
            try:
                return self._session_ask(system, user, json_schema, timeout_s)
            except BrainTimeout:
                raise  # a one-shot retry would double the wait — surface the budget
            except Exception:  # noqa: BLE001 — dead/broken session: degrade to one-shot
                pass
        return run_claude_oneshot(c, system, user, json_schema=json_schema,
                                  timeout_s=timeout_s)

    def _session_ask(self, system: str, user: str, json_schema: str,
                     timeout_s: float | None) -> str:
        c = self.cfg.agent.claude
        key = json_schema
        with self._sessions_lock:
            sess = self._sessions.get(key)
            if sess is not None and (
                not sess.alive()
                or sess.system != system
                or (c.max_session_turns is not None and sess.turns >= c.max_session_turns)
            ):
                # Recycle the child (its system prompt is fixed at spawn) when it has
                # died, the prompt changed (reflection rewrote the learned block —
                # keeping the old child would orphan a live process AND serve a stale
                # prompt), or it hit the turn cap (the conversation grows every turn and
                # latency creeps with it).
                sess.close()
                self._sessions.pop(key, None)
                sess = None
            if sess is None:
                sess = ClaudeSession(c, system, json_schema)
                self._sessions[key] = sess
        try:
            return sess.ask(user, timeout_s)
        except Exception:
            # ask() killed the child on timeout; drop it so the next request starts
            # fresh — but only if a concurrent caller hasn't already replaced it.
            with self._sessions_lock:
                if self._sessions.get(key) is sess:
                    self._sessions.pop(key, None)
            sess.close()
            raise

    @staticmethod
    def _parse(reply: str) -> Decision:
        data = extract_structured(reply)
        if data is None:
            return Decision(action=Action.WAIT, rationale="no_structured_output")
        try:
            return Decision.model_validate(data)
        except Exception:  # noqa: BLE001
            return Decision(action=Action.WAIT, rationale="unparseable_decision")
