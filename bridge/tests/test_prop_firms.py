"""Prop-firm account selection: catalog load/lookup, applying a firm's enforced numbers,
persisting the selection, config wiring, prompt injection, and the server endpoints."""

import yaml
from fastapi.testclient import TestClient

from hermes_bridge.agent_client import DECISION_INSTRUCTION
from hermes_bridge.claude_agent import ClaudeAgentClient
from hermes_bridge.config import BridgeConfig, load_config
from hermes_bridge.prop_firms import (
    apply_account_profile,
    find_account,
    load_catalog,
    persist_account_profile,
)
from hermes_bridge.server import create_app
from tests.conftest import make_session

CATALOG_YAML = """\
firms:
  - name: Topstep
    context_file: topstep.md
    account_types:
      - name: Trading Combine
        accounts:
          - {size: 50000, max_daily_loss: 1000, max_contracts: 5,
             profit_target: 3000, trailing_drawdown: 2000}
          - {size: 100000, max_daily_loss: 2000, max_contracts: 10}
  - name: Apex Trader Funding
    context_file: apex.md
    account_types:
      - name: Evaluation
        accounts:
          - {size: 50000, max_contracts: 10,
             profit_target: 3000, trailing_drawdown: 2500}
"""

TOPSTEP_MARK = "TOPSTEP-RULES-MARK"


def _catalog_file(tmp_path):
    p = tmp_path / "prop-firms.yaml"
    p.write_text(CATALOG_YAML, encoding="utf-8")
    return p


# ---- catalog load + lookup --------------------------------------------------
def test_load_catalog_missing_is_empty(tmp_path):
    assert load_catalog(tmp_path / "nope.yaml").firms == []
    assert load_catalog(None).firms == []


def test_load_catalog_parses_firms(tmp_path):
    cat = load_catalog(_catalog_file(tmp_path))
    assert [f.name for f in cat.firms] == ["Topstep", "Apex Trader Funding"]
    topstep = cat.firms[0]
    assert topstep.context_file == "topstep.md"
    assert [t.name for t in topstep.account_types] == ["Trading Combine"]
    sizes = [a.size for a in topstep.account_types[0].accounts]
    assert sizes == [50000, 100000]


def test_find_account_matches_case_insensitively(tmp_path):
    cat = load_catalog(_catalog_file(tmp_path))
    match = find_account(cat, "topstep", "trading combine", 50000)
    assert match is not None
    firm, prog, tier = match
    assert firm.name == "Topstep" and prog.name == "Trading Combine"
    assert tier.max_daily_loss == 1000 and tier.max_contracts == 5


def test_find_account_miss_returns_none(tmp_path):
    cat = load_catalog(_catalog_file(tmp_path))
    assert find_account(cat, "Topstep", "Trading Combine", 999999) is None  # bad size
    assert find_account(cat, "Topstep", "No Such Program", 50000) is None   # bad type
    assert find_account(cat, "Nope", "Trading Combine", 50000) is None      # bad firm
    assert find_account(cat, None, None, None) is None                      # nothing selected


# ---- applying a firm's enforced numbers -------------------------------------
def test_apply_account_profile_sets_enforced_numbers(tmp_path, cfg):
    cat = load_catalog(_catalog_file(tmp_path))
    _, _, tier = find_account(cat, "Topstep", "Trading Combine", 50000)
    session = make_session(cfg)
    applied = apply_account_profile(cfg, session, tier)
    assert cfg.risk.max_contracts == 5
    assert cfg.daily_goal.max_daily_loss == 1000.0
    assert session.max_daily_loss == 1000.0          # the running session sees it immediately
    # The informational numbers are returned for the UI but NOT enforced.
    assert applied["profit_target"] == 3000 and applied["trailing_drawdown"] == 2000


def test_apply_account_profile_null_daily_loss_left_untouched(tmp_path, cfg):
    # Apex has no daily loss limit → the configured daily loss must be preserved.
    cat = load_catalog(_catalog_file(tmp_path))
    _, _, tier = find_account(cat, "Apex Trader Funding", "Evaluation", 50000)
    session = make_session(cfg)
    before = cfg.daily_goal.max_daily_loss
    apply_account_profile(cfg, session, tier)
    assert cfg.daily_goal.max_daily_loss == before   # unchanged (no firm daily limit)
    assert session.max_daily_loss == before
    assert cfg.risk.max_contracts == 10              # but the contract ceiling still applies


# ---- persistence ------------------------------------------------------------
def test_persist_account_profile_writes_and_preserves_siblings(tmp_path):
    base = tmp_path / "trading.yaml"
    base.write_text("execution:\n  account: Sim101\n", encoding="utf-8")
    local = tmp_path / "trading.local.yaml"
    local.write_text("agent:\n  claude:\n    model: haiku\n", encoding="utf-8")  # pre-existing

    out = persist_account_profile(base, "Topstep", "Trading Combine", 50000)
    assert out == local
    data = yaml.safe_load(local.read_text(encoding="utf-8"))
    assert data["account_profile"] == {
        "prop_firm": "Topstep", "account_type": "Trading Combine", "account_size": 50000}
    # The sibling key already in the local file survives.
    assert data["agent"]["claude"]["model"] == "haiku"


def test_persist_then_load_round_trips(tmp_path):
    base = tmp_path / "trading.yaml"
    base.write_text("strategy_id: rt\n", encoding="utf-8")
    persist_account_profile(base, "Topstep", "Trading Combine", 100000)
    cfg = load_config(base)  # deep-merges the local file we just wrote
    assert cfg.account_profile.prop_firm == "Topstep"
    assert cfg.account_profile.account_type == "Trading Combine"
    assert cfg.account_profile.account_size == 100000


# ---- config wiring ----------------------------------------------------------
def test_account_profile_config_defaults_are_neutral():
    ap = BridgeConfig().account_profile
    assert ap.prop_firm is None and ap.account_type is None and ap.account_size is None
    assert ap.catalog_path == "config/prop-firms.yaml"
    assert ap.context_dir == "hermes/prop-firms"


def test_account_profile_local_override(tmp_path):
    base = tmp_path / "trading.yaml"
    base.write_text("strategy_id: x\n", encoding="utf-8")
    (tmp_path / "trading.local.yaml").write_text(
        "account_profile:\n  prop_firm: Apex Trader Funding\n  account_type: Evaluation\n"
        "  account_size: 50000\n", encoding="utf-8")
    cfg = load_config(base)
    assert cfg.account_profile.prop_firm == "Apex Trader Funding"
    assert cfg.account_profile.account_size == 50000


# ---- prompt injection -------------------------------------------------------
def _client_with_firm(tmp_path, source: str) -> ClaudeAgentClient:
    ctx = tmp_path / "ctx"
    (ctx / "strategies" / "trending").mkdir(parents=True)
    (ctx / "HERMES.md").write_text("FRAMEWORK", encoding="utf-8")
    (ctx / "strategies" / "trending" / "pb.md").write_text("CUSTOM-PB", encoding="utf-8")
    firms = tmp_path / "prop-firms"
    firms.mkdir()
    (firms / "topstep.md").write_text(TOPSTEP_MARK, encoding="utf-8")
    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    cfg.agent.claude.context_dir = str(ctx)
    cfg.strategies.source = source
    cfg.strategies.generated_dir = str(tmp_path / "generated")
    cfg.learning.enabled = False
    cfg.account_profile.context_dir = str(firms)
    return ClaudeAgentClient(cfg)


def test_no_firm_selected_no_block_in_prompt(tmp_path):
    c = _client_with_firm(tmp_path, "agent")
    assert TOPSTEP_MARK not in c._system_prompt(DECISION_INSTRUCTION)


def test_selected_firm_appended_in_agent_mode(tmp_path):
    c = _client_with_firm(tmp_path, "agent")
    c.set_prop_firm_context("topstep.md")
    prompt = c._system_prompt(DECISION_INSTRUCTION)
    assert TOPSTEP_MARK in prompt and "FRAMEWORK" in prompt


def test_selected_firm_appended_in_custom_mode(tmp_path):
    c = _client_with_firm(tmp_path, "custom")
    c.set_prop_firm_context("topstep.md")
    prompt = c._system_prompt(DECISION_INSTRUCTION)
    assert TOPSTEP_MARK in prompt and "CUSTOM-PB" in prompt  # firm rules + on-disk playbook


def test_missing_firm_file_is_silent(tmp_path):
    c = _client_with_firm(tmp_path, "agent")
    c.set_prop_firm_context("ghost.md")  # not on disk
    prompt = c._system_prompt(DECISION_INSTRUCTION)
    assert "FRAMEWORK" in prompt and TOPSTEP_MARK not in prompt  # degrades to framework only


def test_clearing_firm_removes_block(tmp_path):
    c = _client_with_firm(tmp_path, "agent")
    c.set_prop_firm_context("topstep.md")
    assert TOPSTEP_MARK in c._system_prompt(DECISION_INSTRUCTION)
    c.set_prop_firm_context(None)
    assert TOPSTEP_MARK not in c._system_prompt(DECISION_INSTRUCTION)


# ---- server endpoints -------------------------------------------------------
def _server_cfg(tmp_path):
    """A config pointing at a tmp catalog + tmp firm dir, plus a tmp base config path so
    persistence writes into tmp."""
    cfg = BridgeConfig()
    cfg.account_profile.catalog_path = str(_catalog_file(tmp_path))
    firms = tmp_path / "prop-firms"
    firms.mkdir()
    (firms / "topstep.md").write_text(TOPSTEP_MARK, encoding="utf-8")
    cfg.account_profile.context_dir = str(firms)
    base = tmp_path / "trading.yaml"
    base.write_text("strategy_id: hermes-default\n", encoding="utf-8")
    return cfg, str(base)


def test_get_account_profile_returns_catalog(tmp_path):
    cfg, path = _server_cfg(tmp_path)
    c = TestClient(create_app(cfg, config_path=path))
    body = c.get("/account-profile").json()
    assert [f["name"] for f in body["catalog"]["firms"]] == ["Topstep", "Apex Trader Funding"]
    assert body["selected"]["prop_firm"] is None  # nothing selected at startup


def test_post_account_profile_applies_persists_and_surfaces(tmp_path):
    cfg, path = _server_cfg(tmp_path)
    c = TestClient(create_app(cfg, config_path=path))
    r = c.post("/control/account-profile", json={
        "prop_firm": "Topstep", "account_type": "Trading Combine", "account_size": 50000}).json()
    assert r["ok"] is True
    assert r["applied"]["max_daily_loss"] == 1000 and r["applied"]["max_contracts"] == 5
    # Enforced numbers now live in the running config (reflected on /health + /dashboard).
    health = c.get("/health").json()["account_profile"]
    assert health["prop_firm"] == "Topstep" and health["context_file"] == "topstep.md"
    goal = c.get("/dashboard").json()["goal"]
    assert goal["max_daily_loss"] == 1000
    # Persisted to the sibling local file.
    local = yaml.safe_load((tmp_path / "trading.local.yaml").read_text(encoding="utf-8"))
    assert local["account_profile"]["prop_firm"] == "Topstep"


def test_post_account_profile_rejects_unknown(tmp_path):
    cfg, path = _server_cfg(tmp_path)
    c = TestClient(create_app(cfg, config_path=path))
    r = c.post("/control/account-profile", json={
        "prop_firm": "Topstep", "account_type": "Trading Combine", "account_size": 7}).json()
    assert r["ok"] is False and "no matching account" in r["note"]
    assert not (tmp_path / "trading.local.yaml").exists()  # nothing persisted on failure


def test_startup_seeds_configured_profile(tmp_path):
    # A profile configured at startup applies its enforced numbers without a POST.
    cfg, path = _server_cfg(tmp_path)
    cfg.account_profile.prop_firm = "Topstep"
    cfg.account_profile.account_type = "Trading Combine"
    cfg.account_profile.account_size = 50000
    app = create_app(cfg, config_path=path)
    st = app.state.appstate
    assert st.cfg.risk.max_contracts == 5
    assert st.cfg.daily_goal.max_daily_loss == 1000
    assert st.agent.prop_firm_context() == "topstep.md"
