"""The cap-cost bucket: a SHADOWED (over-cap) trigger the brain armed is counterfactually
replayed under kind='over_cap_trigger' / suppressed_by='risk_cap', kept SEPARATE from the
gate-skip 'missed_trigger' bucket — so "did the $125 cap cost a winner?" is answerable on its
own, with the brain's AUTHORED bracket (the faithful 'would it have won at intended risk?')."""

from __future__ import annotations

from hermes_bridge.config import BridgeConfig
from hermes_bridge.engine import TradingEngine
from hermes_bridge.journal import DeclineLog, JournalStore
from hermes_bridge.models import Action, Decision
from hermes_bridge.plan import EntryTrigger, TradePlan
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, synthetic_bars


class _StubAgent:
    def decide(self, req):
        return Decision(action=Action.WAIT, rationale="stub")

    def strategy_source(self):
        return "custom"


class _StubPlanner:
    def __init__(self, plan):
        self._plan = plan

    def current_plan(self):
        return self._plan

    def consume(self, plan):
        self._plan = None

    def schedule_plan_analysis(self, preq):
        pass

    def schedule_session_analysis(self, history, preq, *, force=False):
        pass

    def is_analyzing_session(self):
        return False


def _shadow_plan(basis_ts):
    # Same buy-the-dip band as the gate-skip tests, but flagged un-fillable (over the cap):
    # limit 3995, stop 3991, target 4001 (tick 0.25, 16t stop / 24t target).
    return TradePlan(mode="seek_entry", based_on_bar_ts=basis_ts, triggers=[
        EntryTrigger(direction="long", min_close=3990.0, max_close=3995.0, qty=1,
                     stop_ticks=16, target_ticks=24, confidence=0.4,
                     feasible=False, infeasible_reason="over_cap($500>$250)",
                     rationale="pullback over the cap")])


def _engine(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.counterfactuals_enabled = True
    cfg.learning.counterfactual_horizon_bars = 20
    cfg.learning.declines_path = str(tmp_path / "d.jsonl")
    cfg.strategies.reauthor.enabled = False
    cfg.planner.max_plan_age_bars = 9999
    seed = synthetic_bars(60)
    eng = TradingEngine(
        cfg, BarStore("ES", "5m"),
        SessionState("ES", "5m", 0.25, 12.5, 500, 400),
        _StubAgent(), RiskGate(cfg), planner=_StubPlanner(_shadow_plan(seed[-1].ts)),
        journal=JournalStore(str(tmp_path / "j.jsonl")),
        declines=DeclineLog(str(tmp_path / "d.jsonl")),
    )
    for b in seed:
        eng.store.append(b)
    return eng, seed[-1].ts


def test_shadow_trigger_records_over_cap_bucket(tmp_path):
    eng, t = _engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # close above band -> no fire -> record
    assert len(eng._cf_pending) == 1
    p = eng._cf_pending[0]
    assert p.kind == "over_cap_trigger"          # its OWN bucket, not 'missed_trigger'
    assert p.suppressed_by == "risk_cap"
    assert (p.limit_price, p.stop_price, p.target_price) == (3995.0, 3991.0, 4001.0)


def test_shadow_trigger_resolves_would_win_in_its_bucket(tmp_path):
    eng, t = _engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))   # low 3994 touches 3995 -> fill
    eng.on_bar(make_bar(t + 900, 3997, 4002, 3997, 3998))   # high 4002 >= target 4001 -> win
    rec = eng.declines.all()[-1]
    assert rec["kind"] == "over_cap_trigger"
    assert rec["outcome"] == "would_win"
    assert rec["suppressed_by"] == "risk_cap"
