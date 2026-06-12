from hermes_bridge.agent_client import DECISION_INSTRUCTION, decision_instruction
from hermes_bridge.claude_agent import DECISION_JSON_SCHEMA, decision_json_schema


def test_instruction_mentions_plan_only_when_enabled():
    assert "ARM_PLAN" not in decision_instruction(False)
    on = decision_instruction(True)
    assert "ARM_PLAN" in on
    assert "entry_low" in on and "ttl_bars" in on


def test_schema_gains_plan_only_when_enabled():
    assert "ARM_PLAN" not in decision_json_schema(False)
    s = decision_json_schema(True)
    assert "ARM_PLAN" in s and "entry_low" in s and "ttl_bars" in s


def test_legacy_constants_stay_plan_free():
    # Back-compat: the module-level constants are the plans-off variants.
    assert DECISION_INSTRUCTION == decision_instruction(False)
    assert DECISION_JSON_SCHEMA == decision_json_schema(False)
