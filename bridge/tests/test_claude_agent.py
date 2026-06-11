import json
import subprocess
import types

from hermes_bridge.agent_client import AgentRequest
from hermes_bridge.claude_agent import ClaudeAgentClient
from hermes_bridge.config import BridgeConfig
from hermes_bridge.indicators import build_context
from hermes_bridge.models import Action
from hermes_bridge.session import SessionState
from tests.conftest import synthetic_bars


def _client() -> ClaudeAgentClient:
    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    return ClaudeAgentClient(cfg)


def _req(cfg: BridgeConfig) -> AgentRequest:
    bars = synthetic_bars(120)
    ctx = build_context(bars, ema_fast=cfg.strategy.ema_fast,
                        ema_slow=cfg.strategy.ema_slow, atr_period=cfg.strategy.atr_period)
    sess = SessionState(cfg.instrument.symbol, cfg.instrument.timeframe,
                        cfg.instrument.tick_size, cfg.instrument.tick_value,
                        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss)
    return AgentRequest(mode="seek_entry", context=ctx, recent_bars=bars,
                        account=sess.account_state(mark_price=bars[-1].close))


def test_parse_structured_result_object():
    reply = json.dumps({"is_error": False, "result": {
        "action": "ENTER_LONG", "confidence": 0.7, "qty": 1,
        "stop_ticks": 20, "target_ticks": 40, "rationale": "pullback"}})
    d = ClaudeAgentClient._parse(reply)
    assert d.action is Action.ENTER_LONG
    assert d.confidence == 0.7
    assert d.stop_ticks == 20


def test_parse_text_fenced_result():
    reply = json.dumps({"is_error": False,
                        "result": "thinking...\n```json\n{\"action\":\"WAIT\"}\n```"})
    d = ClaudeAgentClient._parse(reply)
    assert d.action is Action.WAIT


def test_parse_is_error_envelope_waits():
    reply = json.dumps({"is_error": True, "result": "rate limited"})
    assert ClaudeAgentClient._parse(reply).action is Action.WAIT


def test_parse_garbage_waits():
    assert ClaudeAgentClient._parse("not json at all").action is Action.WAIT


def test_decide_builds_isolated_command(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return types.SimpleNamespace(
            stdout=json.dumps({"is_error": False, "result": {"action": "WAIT"}}),
            stderr="", returncode=0)

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.run", fake_run)
    c = _client()
    d = c.decide(_req(c.cfg))
    assert d.action is Action.WAIT
    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--safe-mode" in cmd
    assert "--system-prompt-file" in cmd
    # tool-less, schema-validated, isolated:
    assert "--tools" in cmd and "" in cmd
    assert "--json-schema" in cmd
    assert "--no-session-persistence" in cmd
    # market state goes on stdin, not the argv:
    assert captured["input"].startswith("CURRENT MARKET STATE:")


def test_decide_failsafe_on_timeout(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.run", boom)
    c = _client()
    d = c.decide(_req(c.cfg))
    assert d.action is Action.WAIT
    assert d.rationale.startswith("claude_error:")


def test_build_agent_client_returns_claude():
    from hermes_bridge.agent_client import build_agent_client
    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    assert isinstance(build_agent_client(cfg), ClaudeAgentClient)


def test_parse_structured_output_field():
    # Real `claude --json-schema` envelope: the validated object is in
    # `structured_output`; `result` carries the model's prose. Must read the former.
    reply = json.dumps({
        "is_error": False,
        "result": "Connectivity passed. Standing by.",
        "structured_output": {"action": "WAIT", "confidence": 1, "qty": 0,
                              "rationale": "no signal", "stop_ticks": None,
                              "target_ticks": None},
    })
    d = ClaudeAgentClient._parse(reply)
    assert d.action is Action.WAIT
    assert d.rationale == "no signal"
