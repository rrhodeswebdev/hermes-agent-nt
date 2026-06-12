"""Armed-plan split: arm via ARM_PLAN, rest a limit entry, manage deterministically.

While a plan is armed the agent is never consulted (the StubAgent raises if it is);
TTL, halt, and close-through-zone cancel the plan via CANCEL_ENTRY; a fill consumes
it and journals under the arm rationale even many bars later (extended memo window).
"""

from hermes_bridge.engine import TradingEngine
from hermes_bridge.journal import JournalStore
from hermes_bridge.models import Action, Decision, Fill, PlanSpec, Side
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, synthetic_bars


class StubAgent:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def decide(self, req):
        self.calls += 1
        if self.decision is None:
            raise AssertionError("agent must not be called while a plan is armed")
        return self.decision


def _arm_decision(ttl=3, conf=0.8):
    return Decision(action=Action.ARM_PLAN, confidence=conf, qty=1, stop_ticks=16,
                    target_ticks=24, rationale="arm the pullback",
                    plan=PlanSpec(direction=Side.LONG, entry_low=4000.0,
                                  entry_high=4002.0, ttl_bars=ttl, note="zone"))


def _engine(cfg, tmp_path, agent):
    return TradingEngine(cfg, BarStore("ES", "5m"),
                         SessionState("ES", "5m", 0.25, 12.5, 500, 400),
                         agent, RiskGate(cfg),
                         journal=JournalStore(str(tmp_path / "j.jsonl")))


def _armed_engine(cfg, tmp_path, ttl=3):
    cfg.execution.armed_plans = True
    agent = StubAgent(_arm_decision(ttl=ttl))
    eng = _engine(cfg, tmp_path, agent)
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    res = eng.on_bar(make_bar(bars[-1].ts + 300, 4005, 4006, 4004, 4005))
    return eng, agent, res, bars[-1].ts + 300


def test_arm_emits_limit_entry_and_snapshot(cfg, tmp_path):
    eng, agent, res, ts = _armed_engine(cfg, tmp_path)
    assert res.decision.action == Action.ARM_PLAN
    assert res.command is not None
    assert res.command.action == Action.ENTER_LONG
    assert res.command.limit_price == 4002.0            # zone top for a long
    snap = eng.plan_snapshot()
    assert snap["status"] == "ARMED"
    assert snap["bars_left"] == 3
    assert snap["direction"] == "LONG"


def test_armed_plan_skips_agent_and_expires_by_ttl(cfg, tmp_path):
    eng, agent, res, ts = _armed_engine(cfg, tmp_path, ttl=2)
    agent.decision = None                                # raises if consulted
    r1 = eng.on_bar(make_bar(ts + 300, 4005, 4006, 4004, 4005))    # ttl 2 -> 1
    assert r1.command is None
    assert "plan_armed" in r1.decision.rationale
    r2 = eng.on_bar(make_bar(ts + 600, 4005, 4006, 4004, 4005))    # ttl 1 -> 0: cancel
    assert r2.command is not None
    assert r2.command.action == Action.CANCEL_ENTRY
    assert eng.plan_snapshot() is None
    assert eng._pending_entry is None                    # memo disarmed with the plan


def test_close_through_zone_cancels(cfg, tmp_path):
    eng, agent, res, ts = _armed_engine(cfg, tmp_path)
    agent.decision = None
    r = eng.on_bar(make_bar(ts + 300, 4001, 4001, 3995, 3996))     # close < entry_low
    assert r.command is not None
    assert r.command.action == Action.CANCEL_ENTRY
    assert "invalidated" in r.decision.rationale


def test_halt_cancels_armed_plan(cfg, tmp_path):
    eng, agent, res, ts = _armed_engine(cfg, tmp_path)
    agent.decision = None
    eng.session.halt("manual")
    r = eng.on_bar(make_bar(ts + 300, 4005, 4006, 4004, 4005))
    assert r.command is not None
    assert r.command.action == Action.CANCEL_ENTRY


def test_plan_fill_attributes_after_many_bars(cfg, tmp_path):
    eng, agent, res, ts = _armed_engine(cfg, tmp_path, ttl=8)
    fill_ts = ts + 6 * 300                               # far past freshness + 1 bar
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=4002.0, ts=fill_ts))
    assert eng.plan_snapshot() is None                   # consumed by the fill
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=4003.0, ts=fill_ts + 300))
    recs = eng.journal.all()
    assert len(recs) == 1
    assert recs[0]["rationale"] == "arm the pullback"


def test_arm_flag_off_is_treated_as_wait(cfg, tmp_path):
    cfg.execution.armed_plans = False
    eng = _engine(cfg, tmp_path, StubAgent(_arm_decision()))
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    r = eng.on_bar(make_bar(bars[-1].ts + 300, 4005, 4006, 4004, 4005))
    assert r.command is None
    assert eng.plan_snapshot() is None


def test_arm_respects_min_confidence(cfg, tmp_path):
    cfg.execution.armed_plans = True
    eng = _engine(cfg, tmp_path, StubAgent(_arm_decision(conf=0.1)))  # below 0.55
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    r = eng.on_bar(make_bar(bars[-1].ts + 300, 4005, 4006, 4004, 4005))
    assert r.command is None
    assert eng.plan_snapshot() is None
    assert "low_confidence" in r.decision.rationale


def test_arm_rejects_invalid_zone(cfg, tmp_path):
    cfg.execution.armed_plans = True
    bad = _arm_decision()
    bad.plan = PlanSpec(direction=Side.LONG, entry_low=4002.0, entry_high=4000.0)  # low>high
    eng = _engine(cfg, tmp_path, StubAgent(bad))
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    r = eng.on_bar(make_bar(bars[-1].ts + 300, 4005, 4006, 4004, 4005))
    assert r.command is None
    assert eng.plan_snapshot() is None
    assert "invalid_plan" in r.decision.rationale
