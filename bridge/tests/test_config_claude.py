import pytest
from pydantic import ValidationError

from hermes_bridge.config import BridgeConfig, load_config


def test_load_config_rejects_legacy_hermes_client(tmp_path):
    # The Codex-era value must fail loudly at load — never silently fall back to
    # the mock rules brain, which arms triggers and places orders on its own.
    p = tmp_path / "trading.yaml"
    p.write_text("agent:\n  client: hermes\n")
    with pytest.raises(ValidationError):
        load_config(str(p))


def test_defaults_have_claude_block():
    cfg = BridgeConfig()
    assert cfg.agent.claude.claude_bin == "claude"
    assert cfg.agent.claude.model == "sonnet"
    assert cfg.agent.claude.safe_mode is True
    # Thinking is capped by default so decisions stay fast (and under timeout_s).
    assert cfg.agent.claude.max_thinking_tokens == 0


def test_max_thinking_tokens_override(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text("agent:\n  client: claude\n  claude:\n    max_thinking_tokens: 1024\n")
    cfg = load_config(str(p))
    assert cfg.agent.claude.max_thinking_tokens == 1024


def test_load_config_accepts_claude_client(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text(
        "agent:\n"
        "  client: claude\n"
        "  claude:\n"
        "    model: haiku\n"
        "    timeout_s: 20\n"
    )
    cfg = load_config(str(p))
    assert cfg.agent.client == "claude"
    assert cfg.agent.claude.model == "haiku"
    assert cfg.agent.claude.timeout_s == 20
