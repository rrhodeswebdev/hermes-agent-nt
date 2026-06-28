from hermes_bridge import brain_health
from hermes_bridge.config import BridgeConfig


def test_resilience_defaults_are_neutral():
    cfg = BridgeConfig()
    assert cfg.agent.claude.fallback_models == []
    r = cfg.agent.resilience
    assert r.enabled is False
    assert r.mock_fallback_enabled is False
    assert r.mock_after_consecutive_failures == 3
    assert r.mock_after_seconds_down == 300.0


def test_resilience_loads_from_dict():
    cfg = BridgeConfig.model_validate({
        "agent": {
            "client": "claude",
            "claude": {"fallback_models": ["sonnet", "haiku"]},
            "resilience": {"enabled": True, "mock_fallback_enabled": True,
                           "mock_after_consecutive_failures": 2,
                           "mock_after_seconds_down": 120},
        }
    })
    assert cfg.agent.claude.fallback_models == ["sonnet", "haiku"]
    assert cfg.agent.resilience.enabled is True
    assert cfg.agent.resilience.mock_after_seconds_down == 120.0


def test_mock_brain_status_constant():
    assert brain_health.MOCK == "MOCK"
