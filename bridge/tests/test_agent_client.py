from hermes_bridge.agent_client import MockAgentClient, build_agent_client
from hermes_bridge.config import BridgeConfig
from hermes_bridge.resilient_brain import ResilientBrain


def test_claude_without_resilience_is_bare():
    cfg = BridgeConfig.model_validate({"agent": {"client": "claude"}})
    assert type(build_agent_client(cfg)).__name__ == "ClaudeAgentClient"


def test_claude_with_resilience_is_wrapped():
    cfg = BridgeConfig.model_validate(
        {"agent": {"client": "claude", "resilience": {"enabled": True}}})
    assert isinstance(build_agent_client(cfg), ResilientBrain)


def test_mock_client_unchanged():
    cfg = BridgeConfig.model_validate({"agent": {"client": "mock"}})
    assert isinstance(build_agent_client(cfg), MockAgentClient)
