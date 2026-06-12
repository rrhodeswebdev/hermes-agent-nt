from hermes_bridge.config import BridgeConfig, load_config


def test_prefilter_default_none():
    assert BridgeConfig().agent.prefilter == "none"


def test_reflection_defaults():
    lc = BridgeConfig().learning
    assert lc.reflect_enabled is True
    assert lc.reflect_on_trade_close is True
    assert lc.reflect_model == "sonnet"
    assert lc.max_lessons == 40


def test_prefilter_from_yaml(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("agent:\n  prefilter: mock\nlearning:\n  reflect_enabled: false\n")
    c = load_config(str(p))
    assert c.agent.prefilter == "mock"
    assert c.learning.reflect_enabled is False
