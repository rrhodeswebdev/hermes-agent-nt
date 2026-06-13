"""Agent-authored vs custom strategy source: config, context loading, prompt
assembly, session authoring/persistence, and the NinjaTrader-toggle wiring."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hermes_bridge.agent_client import (
    DECISION_INSTRUCTION,
    load_context_files,
    load_playbook_files,
)
from hermes_bridge.claude_agent import ClaudeAgentClient
from hermes_bridge.config import BridgeConfig, load_config
from hermes_bridge.plan import PlanRequest
from hermes_bridge.server import create_app
from tests.conftest import make_agent_request, synthetic_bars

FRAMEWORK_MARK = "FRAMEWORK-HERMES-MARK"
CUSTOM_MARK = "CUSTOM-TRENDING-PLAYBOOK-MARK"


def _make_ctx(tmp_path):
    """A minimal context dir: two top-level framework files + one regime playbook."""
    ctx = tmp_path / "ctx"
    (ctx / "strategies" / "trending").mkdir(parents=True)
    (ctx / "strategies" / "ranging").mkdir(parents=True)
    (ctx / "HERMES.md").write_text(FRAMEWORK_MARK, encoding="utf-8")
    (ctx / "strategy.md").write_text("decision flow + hard rules", encoding="utf-8")
    (ctx / "strategies" / "trending" / "pb.md").write_text(CUSTOM_MARK, encoding="utf-8")
    return ctx


def _client_with_ctx(tmp_path, source: str) -> ClaudeAgentClient:
    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    cfg.agent.claude.context_dir = str(_make_ctx(tmp_path))
    cfg.strategies.source = source
    cfg.strategies.generated_dir = str(tmp_path / "generated")
    cfg.learning.enabled = False  # keep the system prompt to just framework + strategy
    return ClaudeAgentClient(cfg)


def _preq(cfg):
    ar = make_agent_request(cfg)
    return PlanRequest(
        mode=ar.mode, context=ar.context, recent_bars=ar.recent_bars,
        account=ar.account, bar_ts=ar.recent_bars[-1].ts, assumed_position=0,
    )


# ---- config -----------------------------------------------------------------
def test_strategies_defaults_agent():
    cfg = BridgeConfig()
    assert cfg.strategies.source == "agent"          # the headline feature is the default
    assert cfg.strategies.generated_dir == "hermes/generated"
    assert cfg.strategies.max_chars == 6000


def test_strategies_source_override(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text("strategies:\n  source: custom\n")
    assert load_config(str(p)).strategies.source == "custom"


def test_strategies_source_rejects_unknown(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text("strategies:\n  source: bogus\n")
    with pytest.raises(ValidationError):
        load_config(str(p))


# ---- context loading --------------------------------------------------------
def test_load_context_files_excludes_subdirs(tmp_path):
    ctx = str(_make_ctx(tmp_path))
    full = load_context_files(ctx)
    framework = load_context_files(ctx, include_subdirs=False)
    assert CUSTOM_MARK in full                # default loads the regime playbooks
    assert CUSTOM_MARK not in framework       # framework-only excludes them
    assert FRAMEWORK_MARK in framework        # ...but keeps top-level files


def test_load_playbook_files_only_subdirs(tmp_path):
    ctx = str(_make_ctx(tmp_path))
    pb = load_playbook_files(ctx)
    assert CUSTOM_MARK in pb and FRAMEWORK_MARK not in pb


def test_load_playbook_files_empty_when_no_subdir(tmp_path):
    (tmp_path / "empty").mkdir()
    assert load_playbook_files(str(tmp_path / "empty")) == ""


# ---- prompt assembly --------------------------------------------------------
def test_custom_mode_prompt_uses_on_disk_playbook(tmp_path):
    c = _client_with_ctx(tmp_path, "custom")
    prompt = c._system_prompt(DECISION_INSTRUCTION)
    assert CUSTOM_MARK in prompt and FRAMEWORK_MARK in prompt


def test_agent_mode_prompt_waits_until_authored(tmp_path):
    c = _client_with_ctx(tmp_path, "agent")
    prompt = c._system_prompt(DECISION_INSTRUCTION)
    assert FRAMEWORK_MARK in prompt           # framework still loads
    assert CUSTOM_MARK not in prompt          # on-disk playbooks are NOT used in agent mode
    assert "No strategy has been authored yet" in prompt  # safe WAIT until authored


def test_agent_mode_prompt_includes_authored_playbook(tmp_path):
    c = _client_with_ctx(tmp_path, "agent")
    c._generated_strategy = "## My Authored Setup\nbuy the dip"
    prompt = c._system_prompt(DECISION_INSTRUCTION)
    assert "My Authored Setup" in prompt
    assert "ACTIVE STRATEGY" in prompt


def test_set_strategy_source_switches(tmp_path):
    c = _client_with_ctx(tmp_path, "agent")
    assert c.strategy_source() == "agent"
    c.set_strategy_source("custom")
    assert c.strategy_source() == "custom"
    c.set_strategy_source("garbage")          # ignored
    assert c.strategy_source() == "custom"


# ---- session authoring + persistence ----------------------------------------
def test_author_session_parses_and_persists(tmp_path, fake_claude):
    fake_claude(stdout=json.dumps({
        "is_error": False,
        "structured_output": {
            "playbook": "## VWAP Reclaim\nlong on reclaim",
            "brief": "ranging 5840-5844, low ATR",
        },
    }))
    c = _client_with_ctx(tmp_path, "agent")
    brief = c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert brief == "ranging 5840-5844, low ATR"
    assert "VWAP Reclaim" in (c.generated_strategy() or "")
    # Persisted to disk for review: a per-session file + a stable latest.md.
    latest = tmp_path / "generated" / "latest.md"
    assert latest.exists() and "VWAP Reclaim" in latest.read_text(encoding="utf-8")


def test_author_session_failure_leaves_no_strategy(tmp_path, fake_claude):
    fake_claude(stdout=json.dumps({"is_error": True, "result": "rate limited"}))
    c = _client_with_ctx(tmp_path, "agent")
    brief = c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert brief == ""
    assert c.generated_strategy() is None         # → prompt instructs WAIT, never trades blind


def test_custom_mode_does_not_author(tmp_path, fake_claude):
    # In custom mode the study is the legacy free-text brief; nothing is authored.
    fake_claude(stdout=json.dumps({"is_error": False, "result": "trend up, brief text"}))
    c = _client_with_ctx(tmp_path, "custom")
    brief = c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert brief == "trend up, brief text"
    assert c.generated_strategy() is None


# ---- server wiring (NinjaTrader toggle over /ingest/account) -----------------
def test_health_reports_default_strategy_source(cfg):
    # cfg fixture leaves the default (agent).
    assert TestClient(create_app(cfg)).get("/health").json()["strategy_source"] == "agent"


def test_ingest_account_toggle_flips_source(cfg):
    c = TestClient(create_app(cfg))
    r = c.post("/ingest/account", json={"account": "Sim101", "allow_live": False,
                                        "use_agent_strategies": False})
    assert r.json()["strategy_source"] == "custom"
    assert c.get("/health").json()["strategy_source"] == "custom"
    # Flip back on.
    c.post("/ingest/account", json={"account": "Sim101", "allow_live": False,
                                    "use_agent_strategies": True})
    assert c.get("/health").json()["strategy_source"] == "agent"


def test_ingest_account_omitting_toggle_keeps_source(cfg):
    c = TestClient(create_app(cfg))
    # An account report without the field must not change the source (None = unspecified).
    c.post("/ingest/account", json={"account": "Sim101", "allow_live": False})
    assert c.get("/health").json()["strategy_source"] == "agent"


def test_strategy_endpoint_agent_mode(cfg):
    c = TestClient(create_app(cfg))
    body = c.get("/strategy").json()
    assert body["source"] == "agent"
    assert body["generated"] is False and body["playbook"] is None
