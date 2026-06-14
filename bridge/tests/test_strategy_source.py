"""Agent-authored vs custom strategy source: config, context loading, prompt
assembly, session authoring/persistence, and the NinjaTrader-toggle wiring."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hermes_bridge.agent_client import (
    DECISION_INSTRUCTION,
    load_context_files,
    load_playbook_files,
)
from hermes_bridge.claude_agent import (
    PLAN_INSTRUCTION,
    ClaudeAgentClient,
    render_playbook,
)
from hermes_bridge.config import BridgeConfig, load_config
from hermes_bridge.plan import PlanRequest, TradePlan
from hermes_bridge.server import create_app
from hermes_bridge.views import current_regime, strategy_list_with_active
from tests.conftest import make_agent_request, make_bar, synthetic_bars

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
            "strategies": [{"name": "VWAP Reclaim", "regime": "ranging",
                            "summary": "long on reclaim",
                            "detail": "ENTRY long on reclaim of VWAP; stop below; target high"}],
            "brief": "ranging 5840-5844, low ATR",
        },
    }))
    c = _client_with_ctx(tmp_path, "agent")
    brief = c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert brief == "ranging 5840-5844, low ATR"
    # The binding playbook is RENDERED from the setups, so it carries the setup name + detail.
    playbook = c.generated_strategy() or ""
    assert "VWAP Reclaim" in playbook and "reclaim of VWAP" in playbook
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
    assert body["list"] == [] and body["active_index"] is None  # nothing authored yet


# ---- agent-authored strategies: list capture, normalization, persistence -----
def _author_response(strategies, brief="brief"):
    """`strategies`: a list of {name, regime, summary, detail} dicts, or None to omit the key.
    Setups are the single source of truth now — the binding playbook is rendered from them,
    so there is no separate top-level `playbook` field."""
    body = {"brief": brief}
    if strategies is not None:
        body["strategies"] = strategies
    return json.dumps({"is_error": False, "structured_output": body})


def test_author_session_captures_strategy_list(tmp_path, fake_claude):
    fake_claude(stdout=_author_response([
        {"name": "Trend Pullback", "regime": "trending", "summary": "Buy the higher-low."},
        {"name": "Range Fade", "regime": "ranging", "summary": "Fade the range edges."},
    ]))
    c = _client_with_ctx(tmp_path, "agent")
    c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    setups = c.generated_strategies()
    assert [x["name"] for x in setups] == ["Trend Pullback", "Range Fade"]
    assert [x["regime"] for x in setups] == ["trending", "ranging"]
    # Every setup persisted to the audit-file header.
    latest = (tmp_path / "generated" / "latest.md").read_text(encoding="utf-8")
    assert "Setup: Trend Pullback [trending]" in latest
    assert "Setup: Range Fade [ranging]" in latest


def test_author_session_no_setups_authors_nothing(tmp_path, fake_claude):
    # Setups are the single source of truth: with no usable setups there is no playbook to
    # render, so nothing is authored and the brain WAITs (never trades a fabricated setup).
    fake_claude(stdout=_author_response(None, brief="no clean edge today"))
    c = _client_with_ctx(tmp_path, "agent")
    brief = c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert brief == "no clean edge today"
    assert c.generated_strategies() is None and c.generated_strategy() is None


def test_author_session_drops_unnamed_and_blanks_bad_regime(tmp_path, fake_claude):
    fake_claude(stdout=_author_response([
        {"name": "", "regime": "trending", "summary": "no name → dropped"},
        {"name": "Keeper", "regime": "bogus", "summary": "bad regime → blanked"},
    ]))
    c = _client_with_ctx(tmp_path, "agent")
    c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    setups = c.generated_strategies()
    assert [x["name"] for x in setups] == ["Keeper"]
    assert setups[0]["regime"] == ""        # unrecognized regime blanked, entry kept


def test_author_failure_leaves_strategies_none(tmp_path, fake_claude):
    fake_claude(stdout=json.dumps({"is_error": True, "result": "rate limited"}))
    c = _client_with_ctx(tmp_path, "agent")
    c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert c.generated_strategies() is None


def test_render_playbook_is_single_source_of_truth():
    # The binding playbook is assembled from the setups, so every authored setup's name and
    # full detail appear in the prose the brain trades — the dashboard list (the same setups)
    # and the binding strategy cannot drift apart.
    setups = [
        {"name": "Reclaim 29010", "regime": "trending", "summary": "go with the reclaim",
         "detail": "ENTRY long above 29010; STOP 28995; TARGET 29040"},
        {"name": "Fade 29080", "regime": "ranging", "summary": "fade the highs",
         "detail": "ENTRY short at 29080; STOP 29092; TARGET 29050"},
    ]
    prose = render_playbook(setups)
    for s in setups:
        assert s["name"] in prose and s["detail"] in prose
    assert "[trending]" in prose and "[ranging]" in prose


def test_author_session_binds_playbook_to_setups(tmp_path, fake_claude):
    # End-to-end: the authored list and the binding playbook are derived from the same setups.
    fake_claude(stdout=_author_response([
        {"name": "Opening Drive", "regime": "trending", "summary": "ride the drive",
         "detail": "ENTRY on the opening drive; STOP under the open; TARGET prior day high"}]))
    c = _client_with_ctx(tmp_path, "agent")
    c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    names = [s["name"] for s in (c.generated_strategies() or [])]
    assert names == ["Opening Drive"]
    assert render_playbook(c.generated_strategies()) == c.generated_strategy()


# ---- regime → active-setup mapping (pure helpers) ----------------------------
def test_current_regime_reads_structural_regime():
    # Regime now comes straight from the structural read on the context, not EMAs.
    assert current_regime(SimpleNamespace(regime="trending")) == "trending"
    assert current_regime(SimpleNamespace(regime="ranging")) == "ranging"
    assert current_regime(SimpleNamespace(regime="transitional")) == "transitional"
    assert current_regime(None) is None


def test_strategy_list_with_active_highlights_matching_regime():
    setups = [{"name": "A", "regime": "trending", "summary": "a"},
              {"name": "B", "regime": "ranging", "summary": "b"}]
    items, idx, src = strategy_list_with_active(setups, "ranging")
    assert idx == 1 and src == "regime"
    assert items[1]["active"] is True and items[0]["active"] is False
    # No detectable regime → nothing active.
    items2, idx2, src2 = strategy_list_with_active(setups, None)
    assert idx2 is None and src2 is None and not any(it["active"] for it in items2)
    # Empty list is safe.
    assert strategy_list_with_active(None, "trending") == ([], None, None)


def test_strategy_list_declared_setup_wins_over_regime():
    setups = [{"name": "Trend Pullback", "regime": "trending", "summary": "a"},
              {"name": "Range Fade", "regime": "ranging", "summary": "b"}]
    # Regime says ranging, but the brain declared the trending setup → declared wins.
    items, idx, src = strategy_list_with_active(setups, "ranging", declared="trend pullback")
    assert idx == 0 and src == "declared" and items[0]["active"] is True
    # An unrecognized declared name falls back to the regime match.
    items2, idx2, src2 = strategy_list_with_active(setups, "ranging", declared="Mystery")
    assert idx2 == 1 and src2 == "regime"


# ---- dashboard surfacing: list + active highlight ----------------------------
_TWO_SETUPS = [
    {"name": "Trend Pullback", "regime": "trending", "summary": "Buy pullbacks to the higher-low."},
    {"name": "Range Fade", "regime": "ranging", "summary": "Fade the range edges."},
]


def _claude_app_with_strategies(tmp_path, setups=None, regime=None):
    """A real ClaudeAgentClient app with authored setups installed (no LLM call), and an
    optional detected `regime` (structural) so the active-setup highlight can be exercised."""
    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    cfg.agent.claude.context_dir = str(_make_ctx(tmp_path))
    cfg.strategies.source = "agent"
    cfg.strategies.generated_dir = str(tmp_path / "generated")
    cfg.learning.enabled = False
    app = create_app(cfg)
    st = app.state.appstate
    st.agent._generated_strategy = "## S\nbuy dips"
    st.agent._generated_strategies = list(_TWO_SETUPS if setups is None else setups)
    if regime is not None:
        st.engine.last_context = SimpleNamespace(
            regime=regime, trend="flat", swing_high=None, swing_low=None)
    return app


def test_strategy_endpoint_lists_setups_with_active(tmp_path):
    c = TestClient(_claude_app_with_strategies(tmp_path, regime="ranging"))
    body = c.get("/strategy").json()
    assert body["source"] == "agent" and body["generated"] is True
    assert [x["name"] for x in body["list"]] == ["Trend Pullback", "Range Fade"]
    assert body["regime"] == "ranging" and body["active_index"] == 1
    assert body["list"][1]["active"] is True
    assert body["name"] == "Range Fade"        # headline = the active setup


def test_dashboard_lists_setups_and_highlights_active(tmp_path):
    c = TestClient(_claude_app_with_strategies(tmp_path, regime="trending"))
    strat = c.get("/dashboard").json()["strategy"]
    assert strat["regime"] == "trending" and strat["active_index"] == 0
    assert strat["list"][0]["active"] is True and strat["list"][1]["active"] is False
    assert strat["name"] == "Trend Pullback"   # headline = the active setup


def test_dashboard_no_context_nothing_active(tmp_path):
    strat = TestClient(_claude_app_with_strategies(tmp_path)).get("/dashboard").json()["strategy"]
    assert strat["regime"] is None and strat["active_index"] is None
    assert all(not it["active"] for it in strat["list"])
    assert strat["name"] == "Trend Pullback"   # headline falls back to the first setup


def test_panel_txt_emits_strategy_rows(tmp_path):
    panel = TestClient(
        _claude_app_with_strategies(tmp_path, regime="ranging")).get("/panel.txt").text
    assert "strategy_row=Trend Pullback|trending|Buy pullbacks to the higher-low.|0" in panel
    assert "strategy_row=Range Fade|ranging|Fade the range edges.|1" in panel
    assert "strategy_name=Range Fade" in panel  # headline = active setup (regime ranging)
    assert "strategy_active_source=regime" in panel


def test_dashboard_omits_agent_list_in_custom_mode(tmp_path):
    # After a runtime toggle to custom, the agent client still holds its last authored list;
    # the dashboard must NOT show it as if it were being traded (custom trades the on-disk
    # playbooks, not the authored roster).
    app = _claude_app_with_strategies(tmp_path, regime="ranging")
    app.state.appstate.agent.set_strategy_source("custom")
    strat = TestClient(app).get("/dashboard").json()["strategy"]
    assert strat["source"] == "custom"
    assert strat["list"] == [] and strat["active_index"] is None and strat["name"] is None
    assert "strategy_row=" not in TestClient(app).get("/panel.txt").text


# ---- authoring telemetry (re-author observability) ---------------------------
def _preq_with(cfg, outcome):
    from dataclasses import replace
    return replace(_preq(cfg), outcome=outcome)


def test_authoring_status_none_until_authored(tmp_path):
    assert _client_with_ctx(tmp_path, "agent").authoring_status() is None


def test_authoring_status_counts_and_records_reason(tmp_path, fake_claude):
    fake_claude(stdout=_author_response([
        {"name": "Opening Drive", "regime": "trending", "summary": "ride it",
         "detail": "ENTRY on the drive; STOP under the open; TARGET PDH"}]))
    c = _client_with_ctx(tmp_path, "agent")
    c.analyze_session(_preq_with(c.cfg, "session_start"), synthetic_bars(120))
    st = c.authoring_status()
    assert st["count"] == 1 and st["reason"] == "session_start"
    assert st["authored_at_bar_ts"] is not None
    # A fresh playbook installs on re-author → the count ticks and the reason updates, which is
    # exactly the signal the dashboard now shows so "is it updating?" is no longer a guess.
    c.analyze_session(_preq_with(c.cfg, "reauthor:trend_flip(up->down) x3b"), synthetic_bars(120))
    st2 = c.authoring_status()
    assert st2["count"] == 2 and st2["reason"].startswith("reauthor:trend_flip")


def test_authoring_status_unchanged_when_author_empty(tmp_path, fake_claude):
    # A failed/empty re-author installs no playbook → the count must NOT advance (otherwise the
    # dashboard would imply a refresh that never happened).
    fake_claude(stdout=_author_response([
        {"name": "Keeper", "regime": "trending", "summary": "x", "detail": "ENTRY..."}]))
    c = _client_with_ctx(tmp_path, "agent")
    c.analyze_session(_preq_with(c.cfg, "session_start"), synthetic_bars(120))
    assert c.authoring_status()["count"] == 1
    fake_claude(stdout=_author_response(None, brief="no edge"))   # empty re-author
    c.analyze_session(_preq_with(c.cfg, "reauthor:ceiling(60b)"), synthetic_bars(120))
    assert c.authoring_status()["count"] == 1                     # unchanged


def _app_with_authored(tmp_path, *, count, reason, bars_ago):
    """A claude app with authored setups AND authoring telemetry installed (bypassing the LLM),
    with a last bar placed ``bars_ago`` 5m-bars after the authored-from bar."""
    app = _claude_app_with_strategies(tmp_path, regime="trending")
    st = app.state.appstate
    st.agent._author_count = count
    st.agent._last_author_reason = reason
    last_ts = 1_700_000_000.0
    st.agent._authored_at_bar_ts = last_ts - bars_ago * 300      # 5m default → 300s/bar
    st.store.append(make_bar(last_ts, 5000, 5001, 4999, 5000))
    return app


def test_dashboard_surfaces_authoring_telemetry(tmp_path):
    app = _app_with_authored(tmp_path, count=2, reason="reauthor:trend_flip(up->down) x3b",
                             bars_ago=5)
    authored = TestClient(app).get("/dashboard").json()["strategy"]["authored"]
    assert authored["count"] == 2 and authored["bars_ago"] == 5
    assert authored["reason"].startswith("reauthor:trend_flip")


def test_panel_txt_emits_authoring_and_planner_status(tmp_path):
    app = _app_with_authored(tmp_path, count=3, reason="reauthor:no_setup_for(ranging) x3b",
                             bars_ago=4)
    st = app.state.appstate
    st.planner._status = "analyzing_session"
    st.planner._session_error = "session_analysis:timeout(180s bridge budget)"
    panel = TestClient(app).get("/panel.txt").text
    assert "strategy_authored_count=3" in panel
    assert "strategy_authored_bars_ago=4" in panel
    assert "strategy_authored_reason=reauthor:no_setup_for(ranging) x3b" in panel
    assert "planner_status=analyzing_session" in panel
    assert "session_error=session_analysis:timeout(180s bridge budget)" in panel


def test_dashboard_txt_shows_authored_line(tmp_path):
    app = _app_with_authored(tmp_path, count=3, reason="reauthor:no_setup_for(ranging) x3b",
                             bars_ago=4)
    txt = TestClient(app).get("/dashboard.txt").text
    assert "authored 3×" in txt and "4b ago" in txt


# ---- brain-declared active setup (plan.active_strategy) → highlight -----------
def test_plan_instruction_and_prompt_expose_setup_names(tmp_path):
    assert "setup" in PLAN_INSTRUCTION
    c = _client_with_ctx(tmp_path, "agent")
    c._generated_strategy = "## S\nbuy dips"
    c._generated_strategies = [
        {"name": "Trend Pullback", "regime": "trending", "summary": "x"},
        {"name": "Range Fade", "regime": "ranging", "summary": "y"},
    ]
    prompt = c._system_prompt(PLAN_INSTRUCTION)
    assert "Your setups" in prompt
    assert "Trend Pullback" in prompt and "Range Fade" in prompt


def test_propose_plan_derives_active_from_trigger_setup(tmp_path, fake_claude):
    # active_strategy is derived from the armed trigger's VALIDATED setup, not a free-text
    # field — so the highlighted setup is the one whose condition actually fires.
    fake_claude(stdout=json.dumps({"is_error": False, "structured_output": {
        "bias": "short",
        "triggers": [{"direction": "short", "max_close": 5840.0, "setup": "Range Fade"}]}}))
    c = _client_with_ctx(tmp_path, "agent")
    c._generated_strategies = [
        {"name": "Range Fade", "regime": "ranging", "summary": "x", "detail": "y"}]
    plan = c.propose_plan(_preq(c.cfg))
    assert plan is not None
    assert plan.triggers[0].setup == "Range Fade"
    assert plan.active_strategy == "Range Fade"


def test_propose_plan_normalizes_trigger_setup_casing(tmp_path, fake_claude):
    # A case/whitespace-variant setup name still binds to the canonical roster entry.
    fake_claude(stdout=json.dumps({"is_error": False, "structured_output": {
        "triggers": [{"direction": "short", "max_close": 5840.0, "setup": "  range FADE "}]}}))
    c = _client_with_ctx(tmp_path, "agent")
    c._generated_strategies = [
        {"name": "Range Fade", "regime": "ranging", "summary": "x", "detail": "y"}]
    plan = c.propose_plan(_preq(c.cfg))
    assert plan is not None and plan.triggers[0].setup == "Range Fade"


def test_propose_plan_drops_unknown_trigger_setup(tmp_path, fake_claude):
    # A setup name the brain invents that isn't in the roster is nulled, never guessed.
    fake_claude(stdout=json.dumps({"is_error": False, "structured_output": {
        "triggers": [{"direction": "long", "min_close": 5850.0, "setup": "Mystery Setup"}]}}))
    c = _client_with_ctx(tmp_path, "agent")
    c._generated_strategies = [
        {"name": "Range Fade", "regime": "ranging", "summary": "x", "detail": "y"}]
    plan = c.propose_plan(_preq(c.cfg))
    assert plan is not None
    assert plan.triggers[0].setup is None and plan.active_strategy is None


def test_no_trade_plan_has_no_active_strategy(tmp_path, fake_claude):
    # An empty (no-trade) plan arms no triggers, so no setup is active. Must still validate.
    fake_claude(stdout=json.dumps({"is_error": False, "structured_output": {"triggers": []}}))
    c = _client_with_ctx(tmp_path, "agent")
    plan = c.propose_plan(_preq(c.cfg))
    assert plan is not None and plan.active_strategy is None


def test_dashboard_highlights_brain_declared_setup(tmp_path):
    # Regime says ranging, but the brain's plan declares the trending setup → declared wins.
    app = _claude_app_with_strategies(tmp_path, regime="ranging")
    app.state.appstate.planner.arm(TradePlan(active_strategy="Trend Pullback"))
    strat = TestClient(app).get("/dashboard").json()["strategy"]
    assert strat["active_source"] == "declared" and strat["active_index"] == 0
    assert strat["list"][0]["active"] is True and strat["list"][1]["active"] is False
    assert strat["name"] == "Trend Pullback"


def test_dashboard_unknown_declared_falls_back_to_regime(tmp_path):
    app = _claude_app_with_strategies(tmp_path, regime="trending")
    app.state.appstate.planner.arm(TradePlan(active_strategy="No Such Setup"))
    strat = TestClient(app).get("/dashboard").json()["strategy"]
    assert strat["active_source"] == "regime" and strat["active_index"] == 0


def test_dashboard_reflects_updated_agent_strategies(tmp_path):
    """The dashboard reads the agent's strategies LIVE on every poll (no caching): when the
    agent re-authors a different set, the very next /dashboard call shows the new list."""
    app = _claude_app_with_strategies(tmp_path, setups=[
        {"name": "Morning Range Fade", "regime": "ranging", "summary": "a"}])
    c = TestClient(app)
    assert [s["name"] for s in c.get("/dashboard").json()["strategy"]["list"]] == [
        "Morning Range Fade"]
    # The agent re-authors a different set mid-session (what /control/reauthor installs).
    app.state.appstate.agent._generated_strategies = [
        {"name": "Afternoon Breakout", "regime": "trending", "summary": "b"},
        {"name": "VWAP Reclaim", "regime": "trending", "summary": "c"}]
    assert [s["name"] for s in c.get("/dashboard").json()["strategy"]["list"]] == [
        "Afternoon Breakout", "VWAP Reclaim"]


def test_dashboard_active_highlight_tracks_regime_each_bar(tmp_path):
    """The active highlight is recomputed per request from the latest bar's regime, so it
    follows the market without the strategy list changing."""
    app = _claude_app_with_strategies(tmp_path)  # Trend Pullback (trending) + Range Fade (ranging)
    st = app.state.appstate
    c = TestClient(app)
    st.engine.last_context = SimpleNamespace(
        regime="trending", trend="up", swing_high=None, swing_low=None)
    assert c.get("/dashboard").json()["strategy"]["active_index"] == 0
    # A later bar flips the regime → the highlight follows on the next poll.
    st.engine.last_context = SimpleNamespace(
        regime="ranging", trend="flat", swing_high=None, swing_low=None)
    assert c.get("/dashboard").json()["strategy"]["active_index"] == 1


# ---- /control/reauthor: fresh playbook without a bridge restart ---------------
def test_clear_generated_strategy_resets(tmp_path):
    c = _client_with_ctx(tmp_path, "agent")
    c._generated_strategy = "## S\nx"
    c._generated_strategies = [{"name": "A", "regime": "trending", "summary": "a"}]
    c.clear_generated_strategy()
    assert c.generated_strategy() is None and c.generated_strategies() is None


def test_reauthor_guard_custom_source(tmp_path):
    app = _claude_app_with_strategies(tmp_path)
    app.state.appstate.agent.set_strategy_source("custom")  # the one store → custom
    r = TestClient(app).post("/control/reauthor").json()
    assert r["ok"] is False and "custom" in r["note"]


def test_reauthor_guard_insufficient_history(tmp_path):
    app = _claude_app_with_strategies(tmp_path)  # store is empty
    r = TestClient(app).post("/control/reauthor").json()
    assert r["ok"] is False and "bars" in r["note"]


def test_reauthor_reauthors_fresh_from_history(tmp_path, fake_claude):
    app = _claude_app_with_strategies(tmp_path)
    st = app.state.appstate
    st.planner.synchronous = True                  # run the study inline, deterministically
    st.store.replace_history(synthetic_bars(120))
    # A stale playbook is in place; re-authoring must replace it.
    st.agent._generated_strategy = "## Old Setup\nold"
    st.agent._generated_strategies = [{"name": "Old Setup", "regime": "trending", "summary": "old"}]
    fake_claude(stdout=json.dumps({"is_error": False, "structured_output": {
        "strategies": [{"name": "Reclaim 29010", "regime": "trending", "summary": "new edge",
                        "detail": "ENTRY on reclaim of 29010; stop below; target next level"}],
        "brief": "fresh brief"}}))
    r = TestClient(app).post("/control/reauthor").json()
    assert r["ok"] is True and r["bars"] == 120
    assert [x["name"] for x in (st.agent.generated_strategies() or [])] == ["Reclaim 29010"]
    assert st.planner.session_brief() == "fresh brief"


def test_reauthor_clears_then_authors_even_with_existing_brief(tmp_path, fake_claude):
    # The short-circuit (existing brief ⇒ skip the study) must NOT apply to a manual reauthor.
    app = _claude_app_with_strategies(tmp_path)
    st = app.state.appstate
    st.planner.synchronous = True
    st.planner._brief = "stale brief from an earlier session"
    st.store.replace_history(synthetic_bars(120))
    fake_claude(stdout=json.dumps({"is_error": False, "structured_output": {
        "strategies": [{"name": "Opening Drive", "regime": "trending", "summary": "s",
                        "detail": "ENTRY on opening drive; stop; target"}],
        "brief": "re-authored brief"}}))
    assert TestClient(app).post("/control/reauthor").json()["ok"] is True
    assert st.planner.session_brief() == "re-authored brief"          # study really re-ran
    assert [x["name"] for x in (st.agent.generated_strategies() or [])] == ["Opening Drive"]
