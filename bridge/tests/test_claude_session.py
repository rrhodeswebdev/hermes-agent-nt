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


def test_session_recycled_after_max_turns(monkeypatch):
    # The conversation grows with every turn (and its latency with it): a session
    # at the turn cap must be killed and replaced, not asked again.
    spawned: list[_FakeProc] = []

    def popen(*a, **k):
        proc = _FakeProc([_result_line({"action": "WAIT", "rationale": "session"})])
        spawned.append(proc)
        return proc

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.Popen", popen)
    c = make_claude_client()
    c.cfg.agent.claude.persistent = True
    c.cfg.agent.claude.max_session_turns = 1
    c.decide(make_agent_request(c.cfg))
    c.decide(make_agent_request(c.cfg))
    assert len(spawned) == 2
    assert spawned[0].killed


def test_session_recycled_when_system_prompt_changes(monkeypatch):
    # The system prompt embeds the learned-memory block, which reflection rewrites
    # mid-session. A persistent child's prompt is fixed at spawn, so a changed prompt
    # must recycle the old child in place — NOT spawn a second and orphan the first
    # (one leaked `claude` process + temp files per learned-memory change otherwise).
    spawned: list[_FakeProc] = []

    def popen(*a, **k):
        proc = _FakeProc([_result_line({"action": "WAIT", "rationale": "session"})])
        spawned.append(proc)
        return proc

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.Popen", popen)
    c = make_claude_client()
    c.cfg.agent.claude.persistent = True
    c._session_ask("SYSTEM-A", "u", "{}", None)
    c._session_ask("SYSTEM-B", "u", "{}", None)  # learned block changed -> new prompt
    assert len(spawned) == 2
    assert spawned[0].killed                      # old child closed, not orphaned
    assert len(c._sessions) == 1                  # keyed by schema; no accumulation


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
