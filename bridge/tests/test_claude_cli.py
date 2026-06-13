import json

import pytest

from hermes_bridge.claude_cli import extract_structured, run_claude_oneshot
from hermes_bridge.config import ClaudeClientConfig


def test_extract_structured_prefers_structured_output():
    reply = json.dumps({"is_error": False, "result": "prose",
                        "structured_output": {"a": 1}})
    assert extract_structured(reply) == {"a": 1}


def test_extract_structured_falls_back_to_result_text():
    reply = json.dumps({"is_error": False, "result": "x ```json\n{\"b\":2}\n``` y"})
    assert extract_structured(reply) == {"b": 2}


def test_extract_structured_is_error_returns_none():
    assert extract_structured(json.dumps({"is_error": True, "result": "z"})) is None


def test_extract_structured_garbage_returns_none():
    assert extract_structured("not json") is None


def test_run_claude_oneshot_builds_command(fake_claude):
    captured = fake_claude()
    c = ClaudeClientConfig()
    out = run_claude_oneshot(c, "SYS", "USR", json_schema='{"type":"object"}', model="haiku")
    assert out == "OUT"
    cmd = captured["cmd"]
    assert cmd[0] == "claude" and "-p" in cmd and "--safe-mode" in cmd
    assert "--json-schema" in cmd
    assert "haiku" in cmd
    assert "--system-prompt-file" in cmd
    assert captured["input"] == "USR"


def test_oneshot_nonzero_exit_raises_with_stderr(fake_claude):
    # A hard CLI failure (auth expiry, bad flag) must surface the stderr text —
    # empty stdout would otherwise read downstream as "model returned nothing".
    fake_claude(stdout="", stderr="Invalid API key - please run claude login",
                returncode=1)
    with pytest.raises(RuntimeError, match="Invalid API key"):
        run_claude_oneshot(ClaudeClientConfig(), "SYS", "USR")


def test_oneshot_caps_thinking_via_env(fake_claude):
    captured = fake_claude()
    run_claude_oneshot(ClaudeClientConfig(max_thinking_tokens=0), "SYS", "USR")
    assert captured["env"] is not None
    assert captured["env"]["MAX_THINKING_TOKENS"] == "0"


def test_oneshot_uncapped_inherits_parent_env(fake_claude):
    captured = fake_claude()
    # None → env=None so the subprocess inherits the parent environment unchanged.
    run_claude_oneshot(ClaudeClientConfig(max_thinking_tokens=None), "SYS", "USR")
    assert captured["env"] is None
