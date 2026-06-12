"""Claude agent client — the decision brain via the `claude` CLI on your subscription.

Mirrors HermesAgentClient but talks to Claude Code in headless print mode
(`claude -p --safe-mode`): it authenticates off your Claude subscription (no
ANTHROPIC_API_KEY / metered API) and runs isolated from your global CLAUDE.md,
hooks, MCP, and skills. The trading knowledge lives in the same context/*.md files;
this client frames the request, runs one oneshot, and parses a JSON Decision. Any
failure degrades to WAIT (never auto-trades on a malformed/absent response — open
positions remain protected by the resting bracket in NinjaTrader).
"""

from __future__ import annotations

import json

from .agent_client import (
    DECISION_INSTRUCTION,
    AgentClient,
    AgentRequest,
    build_user_prompt,
    load_context_files,
)
from .claude_cli import extract_structured, run_claude_oneshot
from .config import BridgeConfig
from .journal import JournalStore, select_similar
from .memory import LearnedStore
from .models import Action, Decision

# JSON Schema for `--json-schema`: the Decision shape the agent must return.
DECISION_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["ENTER_LONG", "ENTER_SHORT", "EXIT", "WAIT"]},
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


class ClaudeAgentClient(AgentClient):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._static: str | None = None  # cached static context (rarely changes)
        self._learned = LearnedStore(config.learning.learned_dir)
        self._journal = JournalStore(config.learning.journal_path)

    def decide(self, req: AgentRequest) -> Decision:
        try:
            reply = self._ask(self._system_prompt(), self._user_message(req))
            return self._parse(reply)
        except Exception as exc:  # noqa: BLE001 — fail safe: never auto-trade on error
            return Decision(action=Action.WAIT, rationale=f"claude_error:{type(exc).__name__}")

    def _user_message(self, req: AgentRequest) -> str:
        user = build_user_prompt(req)
        if self.cfg.learning.enabled and self.cfg.learning.retrieve_k > 0:
            similar = select_similar(
                self._journal.recent(200), req.context, self.cfg.learning.retrieve_k)
            if similar:
                user += ("\n\nRELEVANT PAST TRADES (same regime, most recent last):\n"
                         + json.dumps(similar, separators=(",", ":")))
        return user

    def _static_knowledge(self) -> str:
        if self._static is None:
            c = self.cfg.agent.claude
            self._static = load_context_files(c.context_dir) or c.context_hint
        return self._static

    def _system_prompt(self) -> str:
        parts = [self._static_knowledge()]
        if self.cfg.learning.enabled:
            lc = self.cfg.learning
            learned = self._learned.format_for_prompt(
                lc.profile_char_limit, lc.notes_char_limit, lc.lessons_char_limit)
            if learned:
                parts.append(learned)
        parts.append(DECISION_INSTRUCTION)
        return "\n\n".join(parts)

    def _ask(self, system: str, user: str) -> str:
        c = self.cfg.agent.claude
        return run_claude_oneshot(c, system, user, json_schema=DECISION_JSON_SCHEMA)

    @staticmethod
    def _parse(reply: str) -> Decision:
        data = extract_structured(reply)
        if data is None:
            return Decision(action=Action.WAIT, rationale="no_structured_output")
        try:
            return Decision.model_validate(data)
        except Exception:  # noqa: BLE001
            return Decision(action=Action.WAIT, rationale="unparseable_decision")
