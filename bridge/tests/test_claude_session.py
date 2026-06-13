"""Persistent `claude` session: protocol parsing, failure modes, one-shot fallback."""

import io
import json

from hermes_bridge.claude_cli import ClaudeSession
from hermes_bridge.config import ClaudeClientConfig
from hermes_bridge.models import Action
from tests.conftest import make_agent_request, make_claude_client


class _FakeProc:
    """Stands in for the claude child: scripted stdout lines, captured stdin."""

    def __init__(self, stdout_lines: list[str]):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in stdout_lines))
        self.killed = False

    def poll(self):
        return None  # always "alive"; EOF on stdout signals the end instead

    def kill(self):
        self.killed = True


def _result_line(structured: dict) -> str:
    return json.dumps({"type": "result", "is_error": False,
                       "structured_output": structured, "result": ""})


def _session(monkeypatch, stdout_lines: list[str]) -> tuple[ClaudeSession, _FakeProc]:
    proc = _FakeProc(stdout_lines)
    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.Popen",
                        lambda *a, **k: proc)
    return ClaudeSession(ClaudeClientConfig(), "SYS", json_schema="{}"), proc


def test_session_ask_returns_result_envelope(monkeypatch):
    sess, proc = _session(monkeypatch, [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant"}),
        _result_line({"action": "WAIT"}),
    ])
    reply = sess.ask("ping", timeout_s=5)
    env = json.loads(reply)
    assert env["type"] == "result"
    assert env["structured_output"] == {"action": "WAIT"}
    # The turn went out as one stream-json user message.
    sent = json.loads(proc.stdin.getvalue())
    assert sent["type"] == "user"
    assert sent["message"]["content"][0]["text"] == "ping"


def test_session_ask_raises_on_eof(monkeypatch):
    sess, _ = _session(monkeypatch, [json.dumps({"type": "system"})])  # no result
    try:
        sess.ask("ping", timeout_s=5)
        raise AssertionError("expected RuntimeError on EOF")
    except RuntimeError:
        pass


def test_persistent_decide_uses_session_not_oneshot(monkeypatch):
    proc = _FakeProc([_result_line({"action": "WAIT", "rationale": "session"})])
    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.Popen",
                        lambda *a, **k: proc)

    def no_oneshot(*a, **k):
        raise AssertionError("one-shot path must not run while the session works")

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.run", no_oneshot)
    c = make_claude_client()
    c.cfg.agent.claude.persistent = True
    d = c.decide(make_agent_request(c.cfg))
    assert d.action is Action.WAIT
    assert d.rationale == "session"


def test_persistent_decide_falls_back_to_oneshot_on_dead_session(fake_claude, monkeypatch):
    # Session child answers nothing (EOF immediately) → client degrades to the
    # one-shot path for the same request.
    proc = _FakeProc([])
    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.Popen",
                        lambda *a, **k: proc)
    captured = fake_claude(
        stdout=json.dumps({"is_error": False, "result": {"action": "WAIT",
                                                         "rationale": "oneshot"}}))
    c = make_claude_client()
    c.cfg.agent.claude.persistent = True
    d = c.decide(make_agent_request(c.cfg))
    assert d.action is Action.WAIT
    assert d.rationale == "oneshot"
    assert captured["cmd"][0] == "claude"  # the one-shot really ran
