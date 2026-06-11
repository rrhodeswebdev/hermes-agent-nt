import json
import types

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


def test_run_claude_oneshot_builds_command(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return types.SimpleNamespace(stdout="OUT", stderr="", returncode=0)

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.run", fake_run)
    c = ClaudeClientConfig()
    out = run_claude_oneshot(c, "SYS", "USR", json_schema='{"type":"object"}', model="haiku")
    assert out == "OUT"
    cmd = captured["cmd"]
    assert cmd[0] == "claude" and "-p" in cmd and "--safe-mode" in cmd
    assert "--json-schema" in cmd
    assert "haiku" in cmd
    assert "--system-prompt-file" in cmd
    assert captured["input"] == "USR"
