"""Pure system-prompt assembly (prompts.py): the brain's prompt composed from its parts,
asserted directly — no client, no disk."""

from hermes_bridge.prompts import (
    agent_knowledge,
    authored_playbook_block,
    system_prompt,
)


def test_authored_playbook_block_renders_roster():
    block = authored_playbook_block(
        "## Reclaim 28960\nbuy the reclaim",
        [{"name": "Reclaim 28960", "regime": "trending"}, {"name": "Fade 29025", "regime": ""}],
    )
    assert "ACTIVE STRATEGY" in block
    assert "buy the reclaim" in block
    assert "- Reclaim 28960 (trending)" in block
    assert "- Fade 29025" in block               # untagged setup still listed, no (regime) suffix


def test_authored_playbook_block_wait_when_unauthored():
    block = authored_playbook_block(None, None)
    assert "No strategy has been authored yet" in block
    assert "WAIT on every bar" in block


def test_agent_knowledge_separates_framework_and_playbook():
    assert agent_knowledge("FRAMEWORK", "PLAYBOOK") == "FRAMEWORK\n\n---\n\nPLAYBOOK"


def test_system_prompt_drops_empty_learned_block():
    # No stray separator when the learned block is empty (learning disabled).
    assert system_prompt("KNOW", "", "INSTR") == "KNOW\n\nINSTR"
    assert system_prompt("KNOW", "LEARN", "INSTR") == "KNOW\n\nLEARN\n\nINSTR"
