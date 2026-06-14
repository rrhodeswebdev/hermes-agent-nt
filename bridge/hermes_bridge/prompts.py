"""System-prompt assembly for the Claude decision brain — the single home for HOW the
brain's prompt is composed from its parts.

The brain is configured almost entirely by what it is shown, and that composition used to
live inline in `ClaudeAgentClient` interleaved with transport, parsing, and authoring
state — so "what does the brain see this bar?" meant untangling four concerns across one
class. These are pure functions (string in, string out): the client still loads + caches
the knowledge files and gathers live state, then hands the pieces here to be joined. That
makes the assembled prompt assertable directly (right parts, right order, no stray
separators) without standing up a client or touching disk.
"""

from __future__ import annotations

# The ACTIVE STRATEGY block headers (agent mode).
_ACTIVE_STRATEGY_HEADER = (
    "=== ACTIVE STRATEGY (you authored this from the pre-session history "
    "study — it is binding for this session) ===\n"
)
_NO_STRATEGY_BLOCK = (
    "=== ACTIVE STRATEGY ===\n"
    "No strategy has been authored yet (the pre-session study has not produced "
    "one). Until one is in place, WAIT on every bar and arm NO entry triggers."
)


def authored_playbook_block(
    generated_strategy: str | None, generated_strategies: list[dict] | None
) -> str:
    """The ACTIVE STRATEGY block (agent mode): the authored playbook prose plus a roster of
    the canonical setup names — so the brain tags each trigger's ``setup`` with an EXACT
    string the bridge validates and the dashboard matches. When nothing has been authored,
    a WAIT instruction so the brain arms no triggers until a playbook exists."""
    if not generated_strategy:
        return _NO_STRATEGY_BLOCK
    roster = ""
    if generated_strategies:
        header = "\n\nYour setups (set each trigger's `setup` to the one it trades):\n"
        roster = header + "\n".join(
            f"- {s['name']}" + (f" ({s['regime']})" if s.get("regime") else "")
            for s in generated_strategies
        )
    return _ACTIVE_STRATEGY_HEADER + generated_strategy + roster


def agent_knowledge(framework: str, authored_block: str) -> str:
    """Agent-mode knowledge block: the framework files, then the brain's authored playbook
    (or the WAIT-until-authored block), separated by a horizontal rule."""
    return framework + "\n\n---\n\n" + authored_block


def system_prompt(knowledge: str, learned: str, instruction: str) -> str:
    """Join the system-prompt sections — the knowledge block, the (optional) learned-memory
    block, and the task instruction — with the standard blank-line separator. Empty sections
    are dropped, so a disabled learned block leaves no stray separator."""
    return "\n\n".join(s for s in (knowledge, learned, instruction) if s)
