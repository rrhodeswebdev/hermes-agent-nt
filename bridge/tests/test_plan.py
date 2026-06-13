"""Pre-armed plan flow: trigger semantics, planner state, and the engine cycle.

The contract under test: bar closes are answered instantly from the plan armed
by the PREVIOUS analysis (enter with the pre-computed bracket / exit / wait),
and the next analysis is scheduled off the critical path.
"""

import json

from hermes_bridge.agent_client import MockAgentClient, build_agent_client
from hermes_bridge.config import BridgeConfig
from hermes_bridge.engine import TradingEngine
from hermes_bridge.models import Action, Bar, Fill, Side
from hermes_bridge.plan import (
    EntryTrigger,
    ExitRule,
    Planner,
    PlanRequest,
    TradePlan,
    evaluate_plan,
)
from hermes_bridge.replay_sim import ReplaySimulator
from hermes_bridge.risk import RiskGate
from hermes_bridge.store import BarStore
from tests.conftest import (
    make_agent_request,
    make_claude_client,
    make_session,
    synthetic_bars,
)
from tests.conftest import (
    make_close_bar as _bar,
)


def _preq(cfg: BridgeConfig, mode: str = "seek_entry", assumed: int = 0,
          bars: list[Bar] | None = None) -> PlanRequest:
    bars = bars or synthetic_bars(200)
    req = make_agent_request(cfg, mode=mode, bars=bars)
    return PlanRequest(mode=req.mode, context=req.context, recent_bars=req.recent_bars,
                       account=req.account, bar_ts=bars[-1].ts, assumed_position=assumed)


# --------------------------------------------------------------------------- #
# Trigger / exit-rule semantics                                                #
# --------------------------------------------------------------------------- #
def test_entry_trigger_bounds():
    band = EntryTrigger(direction="long", min_close=4000.0, max_close=4002.0)
    assert band.matches(4000.0) and band.matches(4002.0)
    assert not band.matches(3999.75) and not band.matches(4002.25)
    breakout = EntryTrigger(direction="long", min_close=4100.0)
    assert breakout.matches(4100.0) and not breakout.matches(4099.75)
    # An unconditional trigger must never fire.
    assert not EntryTrigger(direction="long").matches(4000.0)


def test_zero_qty_trigger_never_fires():
    # A trigger that would buy 0 contracts must not fire — otherwise _to_command
    # coerces the 0 up to a surprise 1-lot order.
    band = EntryTrigger(direction="long", min_close=4000.0, qty=0)
    assert not band.matches(4000.0)
    plan = TradePlan(mode="seek_entry", triggers=[band])
    assert evaluate_plan(plan, _bar(1, 4000.0), position=0).action is Action.WAIT


def test_exit_rule_bounds():
    rule = ExitRule(exit_below=3990.0, exit_above=4010.0)
    assert rule.matches(3990.0) and rule.matches(4010.0)
    assert not rule.matches(4000.0)
    assert not ExitRule().matches(4000.0)


def test_evaluate_plan_entry_fires_with_bracket():
    plan = TradePlan(mode="seek_entry", triggers=[EntryTrigger(
        direction="long", min_close=4000.0, qty=2, stop_ticks=10, target_ticks=20,
        confidence=0.8, rationale="breakout hold")])
    d = evaluate_plan(plan, _bar(1, 4001.0), position=0)
    assert d.action is Action.ENTER_LONG
    assert d.qty == 2 and d.stop_ticks == 10 and d.target_ticks == 20
    assert d.confidence == 0.8


def test_evaluate_plan_no_trigger_waits():
    plan = TradePlan(mode="seek_entry", triggers=[EntryTrigger(
        direction="short", max_close=3990.0, stop_ticks=8, target_ticks=16)])
    d = evaluate_plan(plan, _bar(1, 4000.0), position=0)
    assert d.action is Action.WAIT
    assert "no_trigger" in d.rationale


def test_evaluate_plan_manage_exit_and_hold():
    plan = TradePlan(mode="manage_position",
                     exit=ExitRule(exit_below=3995.0, rationale="trend flip"))
    assert evaluate_plan(plan, _bar(1, 3994.0), position=1).action is Action.EXIT
    held = evaluate_plan(plan, _bar(1, 4000.0), position=1)
    assert held.action is Action.WAIT and "in_trade_hold" in held.rationale


# --------------------------------------------------------------------------- #
# Planner state                                                                #
# --------------------------------------------------------------------------- #
class _LyingAgent(MockAgentClient):
    """Returns a plan with the WRONG mode/basis — the planner must stamp both."""

    def propose_plan(self, preq: PlanRequest) -> TradePlan:
        return TradePlan(mode="seek_entry", based_on_bar_ts=999_999.0, rationale="lies")


def test_planner_stamps_mode_and_basis(cfg):
    planner = Planner(cfg, _LyingAgent(cfg), synchronous=True)
    planner.schedule_plan_analysis(_preq(cfg, mode="manage_position", assumed=1))
    p = planner.current_plan()
    assert p is not None
    assert p.mode == "manage_position"           # not the lie
    assert p.based_on_bar_ts != 999_999.0        # stamped from the request bar


def test_planner_never_clobbers_newer_plan(cfg):
    planner = Planner(cfg, MockAgentClient(cfg), synchronous=True)
    planner.arm(TradePlan(based_on_bar_ts=200.0, rationale="newer"))
    planner.arm(TradePlan(based_on_bar_ts=100.0, rationale="older"))
    assert planner.current_plan().rationale == "newer"


def test_planner_failure_keeps_prior_plan(cfg):
    class Boom(MockAgentClient):
        def propose_plan(self, preq):
            raise RuntimeError("llm down")

    planner = Planner(cfg, Boom(cfg), synchronous=True)
    planner.arm(TradePlan(based_on_bar_ts=100.0, rationale="armed"))
    planner.schedule_plan_analysis(_preq(cfg))
    assert planner.current_plan().rationale == "armed"
    assert planner.snapshot()["last_error"].startswith("plan_analysis:")


def test_planner_timeout_error_names_the_bridge_budget(cfg):
    """A brain timeout must surface the exceeded planner budget on the dashboard,
    not a bare exception name that reads like the NinjaTrader HTTP timeout."""
    from hermes_bridge.models import BrainTimeout

    class Slow(MockAgentClient):
        def propose_plan(self, preq):
            raise BrainTimeout(75.0)

    planner = Planner(cfg, Slow(cfg), synchronous=True)
    planner.schedule_plan_analysis(_preq(cfg))
    assert planner.snapshot()["last_error"] == "plan_analysis:timeout(75s bridge budget)"


def test_session_analysis_stores_brief_and_arms_initial_plan(cfg):
    planner = Planner(cfg, MockAgentClient(cfg), synchronous=True)
    bars = synthetic_bars(200)
    planner.schedule_session_analysis(bars, _preq(cfg, bars=bars))
    assert "trend=" in planner.session_brief()
    assert planner.current_plan() is not None
    assert planner.current_plan().based_on_bar_ts == bars[-1].ts


# --------------------------------------------------------------------------- #
# Engine cycle: instant close evaluation + follow-up analysis                  #
# --------------------------------------------------------------------------- #
class _StubAgent(MockAgentClient):
    """Deterministic plans: always-firing long entry; always-firing exit."""

    def propose_plan(self, preq: PlanRequest) -> TradePlan:
        if preq.mode == "manage_position":
            return TradePlan(mode="manage_position",
                             exit=ExitRule(exit_below=1e9, rationale="always-exit"))
        return TradePlan(mode="seek_entry", bias="long", triggers=[EntryTrigger(
            direction="long", min_close=0.0, qty=1, stop_ticks=8, target_ticks=16,
            confidence=0.9, rationale="always")])


def _engine(cfg, agent):
    session = make_session(cfg)
    planner = Planner(cfg, agent, synchronous=True)
    engine = TradingEngine(cfg, BarStore("ES", "5m"), session, agent,
                           RiskGate(cfg), planner=planner)
    return engine, session, planner


def test_plan_cycle_enter_manage_exit(cfg):
    engine, session, planner = _engine(cfg, _StubAgent(cfg))
    bars = synthetic_bars(4)

    # Bar 0: nothing armed yet → instant WAIT; the follow-up analysis arms a plan.
    r0 = engine.on_bar(bars[0])
    assert r0.decision.action is Action.WAIT
    assert r0.decision.rationale.startswith("no_plan")
    assert planner.current_plan() is not None

    # Bar 1: armed trigger fires mechanically — pre-computed bracket, no analysis.
    r1 = engine.on_bar(bars[1])
    assert r1.command is not None and r1.command.action is Action.ENTER_LONG
    assert r1.command.stop_ticks == 8 and r1.command.target_ticks == 16
    # Follow-up was scheduled optimistically in manage mode for the next close.
    assert planner.current_plan().mode == "manage_position"

    # Fill arrives; the in-trade close is answered from the armed exit rule.
    engine.on_fill(Fill(side=Side.LONG, qty=1, price=bars[1].close, ts=bars[1].ts))
    r2 = engine.on_bar(bars[2])
    assert r2.command is not None and r2.command.action is Action.EXIT
    assert r2.command.qty == 1
    # And the next analysis goes back to hunting entries.
    assert planner.current_plan().mode == "seek_entry"


def test_plan_mode_mismatch_self_corrects(cfg):
    engine, session, planner = _engine(cfg, _StubAgent(cfg))
    bars = synthetic_bars(5)
    engine.on_bar(bars[0])                      # arms the entry plan
    r1 = engine.on_bar(bars[1])                 # entry command queued...
    assert r1.command is not None
    # ...but the fill NEVER arrives: the optimistic manage plan mismatches the
    # flat position → instant WAIT, and the follow-up re-arms for seek_entry.
    r2 = engine.on_bar(bars[2])
    assert r2.decision.action is Action.WAIT
    assert r2.decision.rationale.startswith("plan_mode_mismatch")
    assert planner.current_plan().mode == "seek_entry"
    r3 = engine.on_bar(bars[3])
    assert r3.command is not None and r3.command.action is Action.ENTER_LONG


def test_in_trade_close_waits_instantly_without_exit_condition(cfg):
    class HoldAgent(_StubAgent):
        def propose_plan(self, preq: PlanRequest) -> TradePlan:
            if preq.mode == "manage_position":
                return TradePlan(mode="manage_position",
                                 exit=ExitRule(exit_below=0.0, rationale="never"))
            return super().propose_plan(preq)

    engine, session, planner = _engine(cfg, HoldAgent(cfg))
    bars = synthetic_bars(5)
    engine.on_bar(bars[0])
    engine.on_bar(bars[1])
    engine.on_fill(Fill(side=Side.LONG, qty=1, price=bars[1].close, ts=bars[1].ts))
    r2 = engine.on_bar(bars[2])
    assert r2.decision.action is Action.WAIT
    assert "in_trade_hold" in r2.decision.rationale
    assert r2.command is None


def test_stale_plan_is_discarded(cfg):
    class OneShot(MockAgentClient):
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0

        def propose_plan(self, preq: PlanRequest) -> TradePlan | None:
            self.calls += 1
            if self.calls > 1:
                return None  # analysis goes dark after the first plan
            return TradePlan(mode="seek_entry", triggers=[EntryTrigger(
                direction="long", min_close=1e9, stop_ticks=8, target_ticks=16)])

    assert cfg.planner.max_plan_age_bars == 2
    engine, session, planner = _engine(cfg, OneShot(cfg))
    bars = synthetic_bars(6)
    engine.on_bar(bars[0])                                   # arms (basis bar 0)
    assert "no_trigger" in engine.on_bar(bars[1]).decision.rationale   # age 1
    # Config promise: "a plan based on a bar this many closes old no longer fires".
    r2 = engine.on_bar(bars[2])                                        # age 2 → stale
    assert r2.decision.rationale.startswith("plan_stale")


def test_fired_trigger_is_consumed_never_refires_before_fill(cfg):
    """The doubling bug: trigger fires, the fill is lost/delayed (position still 0)
    and the follow-up analysis returns nothing — the SAME plan must not fire again
    on the next close and queue a second entry."""
    cfg.planner.max_plan_age_bars = 5  # keep the plan fresh so consume is what blocks

    class OneShotFire(MockAgentClient):
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0

        def propose_plan(self, preq: PlanRequest) -> TradePlan | None:
            self.calls += 1
            if self.calls > 1:
                return None  # analysis goes dark after arming once
            return TradePlan(mode="seek_entry", triggers=[EntryTrigger(
                direction="long", min_close=0.0, qty=1, stop_ticks=8,
                target_ticks=16, confidence=0.9)])

    engine, session, planner = _engine(cfg, OneShotFire(cfg))
    bars = synthetic_bars(4)
    engine.on_bar(bars[0])                       # arms (basis bar 0)
    r1 = engine.on_bar(bars[1])
    assert r1.command is not None and r1.command.action is Action.ENTER_LONG
    r2 = engine.on_bar(bars[2])                  # no fill arrived; close still in band
    assert r2.command is None
    assert r2.decision.action is Action.WAIT


def test_session_failure_survives_subsequent_arm(cfg):
    """A failed pre-session study must stay visible after the inline plan arms
    (arm() clears last_error): every plan that day runs without the brief."""

    class BoomSession(MockAgentClient):
        def analyze_session(self, preq, history):
            raise RuntimeError("study blew up")

    planner = Planner(cfg, BoomSession(cfg), synchronous=True)
    bars = synthetic_bars(200)
    planner.schedule_session_analysis(bars, _preq(cfg, bars=bars))
    snap = planner.snapshot()
    assert snap["status"] == "armed"             # the inline plan still armed
    assert snap["last_error"] == ""              # ...and cleared the generic error
    assert snap["session_error"].startswith("session_analysis:")


def test_session_restudy_skipped_when_brief_exists(cfg):
    """A mid-session reconnect re-posts history; the study must not re-run (it
    would blind the plan cycle for minutes) — only the plan refreshes."""
    calls = {"session": 0, "plan": 0}

    class Counting(MockAgentClient):
        def analyze_session(self, preq, history):
            calls["session"] += 1
            return super().analyze_session(preq, history)

        def propose_plan(self, preq):
            calls["plan"] += 1
            return super().propose_plan(preq)

    planner = Planner(cfg, Counting(cfg), synchronous=True)
    bars = synthetic_bars(200)
    planner.schedule_session_analysis(bars, _preq(cfg, bars=bars))
    planner.schedule_session_analysis(bars, _preq(cfg, bars=bars))
    assert calls["session"] == 1
    assert calls["plan"] == 2                    # the reconnect still refreshes the plan


def test_plan_based_on_current_bar_not_active_yet(cfg):
    engine, session, planner = _engine(cfg, _StubAgent(cfg))
    bars = synthetic_bars(3)
    planner.arm(TradePlan(mode="seek_entry", based_on_bar_ts=bars[0].ts,
                          triggers=[EntryTrigger(direction="long", min_close=0.0,
                                                 stop_ticks=8, target_ticks=16)]))
    r = engine.on_bar(bars[0])  # plan basis == this bar → can't fire on itself
    assert r.decision.action is Action.WAIT
    assert r.decision.rationale == "plan_not_yet_active"


def test_low_confidence_trigger_suppressed(cfg):
    class TimidAgent(_StubAgent):
        def propose_plan(self, preq: PlanRequest) -> TradePlan:
            plan = super().propose_plan(preq)
            for t in plan.triggers:
                t.confidence = 0.1
            return plan

    engine, session, planner = _engine(cfg, TimidAgent(cfg))
    bars = synthetic_bars(3)
    engine.on_bar(bars[0])
    r1 = engine.on_bar(bars[1])
    assert r1.command is None
    assert r1.decision.rationale.startswith("low_confidence")


# --------------------------------------------------------------------------- #
# Mock agent plans + full replay                                               #
# --------------------------------------------------------------------------- #
def test_mock_arms_long_pullback_trigger_in_uptrend(cfg):
    preq = _preq(cfg)
    assert preq.context.trend == "up"
    plan = MockAgentClient(cfg).propose_plan(preq)
    assert plan.bias == "long" and len(plan.triggers) == 1
    t = plan.triggers[0]
    assert t.direction == "long"
    assert t.min_close is not None and t.max_close is not None
    assert t.stop_ticks >= 1 and t.target_ticks >= 1


def test_mock_manage_plan_arms_trend_flip_exit(cfg):
    preq = _preq(cfg, mode="manage_position", assumed=1)
    plan = MockAgentClient(cfg).propose_plan(preq)
    assert plan.mode == "manage_position"
    assert plan.triggers == []
    assert plan.exit is not None and plan.exit.exit_below == preq.context.swing_low


def test_replay_with_planner_finds_entries(cfg):
    assert cfg.planner.enabled  # default-on: replay exercises the planned cycle
    sim = ReplaySimulator(cfg)
    report = sim.run(synthetic_bars(400), warmup=50)
    assert report.entries > 0
    assert report.trades_today <= cfg.risk.max_trades_per_day


# --------------------------------------------------------------------------- #
# Claude client: plan parsing + session brief                                  #
# --------------------------------------------------------------------------- #
def test_claude_propose_plan_parses_structured_output(fake_claude):
    plan_obj = {
        "bias": "long",
        "triggers": [{"direction": "long", "min_close": 4000.5, "max_close": None,
                      "qty": 1, "stop_ticks": 10, "target_ticks": 20,
                      "confidence": 0.7, "rationale": "breakout hold"}],
        "exit": None,
        "rationale": "uptrend continuation",
    }
    captured = fake_claude(json.dumps({"is_error": False, "structured_output": plan_obj}))
    c = make_claude_client()
    plan = c.propose_plan(_preq(c.cfg))
    assert plan is not None and plan.bias == "long"
    assert plan.triggers[0].min_close == 4000.5
    assert "--json-schema" in captured["cmd"]
    assert captured["input"].startswith("SESSION BRIEF")
    assert captured["timeout"] == c.cfg.planner.plan_timeout_s


def test_claude_propose_plan_garbage_returns_none(fake_claude):
    fake_claude("totally not json")
    c = make_claude_client()
    assert c.propose_plan(_preq(c.cfg)) is None


def test_claude_propose_plan_rejects_json_without_triggers(fake_claude):
    # Scraped JSON lacking the schema-required "triggers" key must not validate
    # into an all-defaults plan (which would arm — and in manage mode replace a
    # real exit rule with "hold").
    fake_claude(json.dumps({"is_error": False, "result": 'note: {"note":"hi"}'}))
    c = make_claude_client()
    assert c.propose_plan(_preq(c.cfg)) is None


def test_claude_session_brief_is_free_text(fake_claude):
    captured = fake_claude(
        json.dumps({"is_error": False, "result": "Regime: trending up. Key levels..."}))
    c = make_claude_client()
    c.set_strategy_source("custom")  # the free-text brief is the custom-mode study
    brief = c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    assert brief.startswith("Regime: trending up")
    assert "--json-schema" not in captured["cmd"]
    assert captured["input"].startswith("HISTORICAL DATA")
    assert captured["timeout"] == c.cfg.planner.session_timeout_s


def test_claude_session_study_uses_session_model(fake_claude):
    # The one-time history study runs on session_model; the per-bar plan
    # analysis stays on the (faster) decision model.
    captured = fake_claude(json.dumps({"is_error": False, "result": "brief"}))
    c = make_claude_client()
    c.cfg.agent.claude.model = "haiku"
    c.cfg.agent.claude.session_model = "sonnet"

    c.analyze_session(_preq(c.cfg), synthetic_bars(120))
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"

    c.propose_plan(_preq(c.cfg))
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_build_agent_client_plans_are_optional_for_base(cfg):
    # Any AgentClient that doesn't implement planning degrades safely to None/"".
    agent = build_agent_client(cfg)
    assert isinstance(agent, MockAgentClient)


def test_build_agent_client_rejects_unknown_client(cfg):
    import pytest

    # Assignment (the HERMES_BRIDGE_AGENT env override) bypasses config validation;
    # a stale legacy value must error, never silently trade with the mock brain.
    cfg.agent.client = "hermes"
    with pytest.raises(ValueError, match="hermes"):
        build_agent_client(cfg)
