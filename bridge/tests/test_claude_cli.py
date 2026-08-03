import json

import pytest

from hermes_bridge.claude_cli import (
    _fallback_model_args,
    extract_structured,
    run_claude_oneshot,
)
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


def test_fallback_args_empty_when_unset():
    assert _fallback_model_args(ClaudeClientConfig()) == []


def test_fallback_args_csv_when_set():
    c = ClaudeClientConfig(fallback_models=["sonnet", "haiku"])
    assert _fallback_model_args(c) == ["--fallback-model", "sonnet,haiku"]


def test_oneshot_includes_fallback_model_when_set(fake_claude):
    captured = fake_claude()
    c = ClaudeClientConfig(fallback_models=["sonnet", "haiku"])
    run_claude_oneshot(c, "SYS", "USR")
    cmd = captured["cmd"]
    assert "--fallback-model" in cmd
    assert cmd[cmd.index("--fallback-model") + 1] == "sonnet,haiku"


def test_oneshot_omits_fallback_model_when_unset(fake_claude):
    captured = fake_claude()
    run_claude_oneshot(ClaudeClientConfig(), "SYS", "USR")
    assert "--fallback-model" not in captured["cmd"]


# --- Timeout path must never hang. subprocess.run kills only the DIRECT child on timeout
# and then calls communicate() with NO timeout; the CLI spawns node.exe grandchildren that
# inherit the stdout pipe, so a survivor keeps the pipe from reaching EOF and that second
# communicate() blocks forever. Observed live: a claude.exe child of the bridge idled 30h
# (and again ~6h), stalling the consolidation thread behind it (2026-08-02).
import subprocess  # noqa: E402

from hermes_bridge.claude_cli import BrainTimeout, _run_capture  # noqa: E402


class _HangingProc:
    """First communicate() times out; the drain would block forever if unbounded."""

    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.calls = 0
        self.drain_timeouts: list = []

    def communicate(self, input=None, timeout=None):  # noqa: A002
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        self.drain_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    def kill(self):
        pass


def test_run_capture_timeout_kills_tree_and_bounds_the_drain(monkeypatch):
    proc = _HangingProc()
    killed: list = []
    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.Popen", lambda *a, **k: proc)
    monkeypatch.setattr("hermes_bridge.claude_cli._kill_tree", lambda p: killed.append(p.pid))

    with pytest.raises(subprocess.TimeoutExpired):
        _run_capture(["claude"], input="x", env=None, timeout=1.0)

    assert killed == [4242], "the whole process TREE must be killed, not just the child"
    assert proc.calls == 2, "the pipe must still be drained after the kill"
    assert proc.drain_timeouts and all(t is not None for t in proc.drain_timeouts), \
        "the post-kill drain MUST be bounded — an unbounded one is the original hang"


def test_oneshot_surfaces_timeout_as_braintimeout(monkeypatch):
    monkeypatch.setattr(
        "hermes_bridge.claude_cli._run_capture",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("claude", 5.0)),
    )
    with pytest.raises(BrainTimeout):
        run_claude_oneshot(ClaudeClientConfig(), "sys", "user", timeout_s=5.0)
