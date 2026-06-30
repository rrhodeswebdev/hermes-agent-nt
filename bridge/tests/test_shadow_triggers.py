"""Plan-time shadowing of un-fillable (over-cap) triggers: a trigger whose stop would bust the
RiskGate's single-contract cap is flagged feasible=False — shown + (later) counterfactually
replayed, but never fired. Only active when risk.shadow_infeasible_triggers is on."""

from hermes_bridge.agent_client import MockAgentClient
from hermes_bridge.models import Action
from hermes_bridge.plan import EntryTrigger, Planner, PlanRequest, TradePlan, evaluate_plan
from tests.conftest import make_agent_request, synthetic_bars
from tests.conftest import make_close_bar as _bar


def _preq(cfg, mode="seek_entry", assumed=0, bars=None):
    bars = bars or synthetic_bars(200)
    req = make_agent_request(cfg, mode=mode, bars=bars)
    return PlanRequest(mode=req.mode, context=req.context, recent_bars=req.recent_bars,
                       account=req.account, bar_ts=bars[-1].ts, assumed_position=assumed)


class _TwoTriggerAgent(MockAgentClient):
    """One fillable trigger (tight stop) + one over-cap trigger (wide stop). ES fixture:
    tick_value $12.50, max_risk_per_trade $250 → break-even is 20 ticks."""

    def propose_plan(self, preq: PlanRequest) -> TradePlan:
        return TradePlan(mode="seek_entry", bias="long", triggers=[
            EntryTrigger(direction="long", min_close=4000.0, max_close=4002.0,
                         stop_ticks=8, target_ticks=16),    # 8*$12.50=$100 ≤ $250 → fillable
            EntryTrigger(direction="long", min_close=3990.0, max_close=3992.0,
                         stop_ticks=40, target_ticks=16),   # 40*$12.50=$500 > $250 → over cap
        ])


def test_shadow_trigger_never_fires():
    shadow = EntryTrigger(direction="long", min_close=4000.0, stop_ticks=40,
                          feasible=False, infeasible_reason="over_cap($500>$250)")
    assert not shadow.matches(4001.0)                      # in-band, but shadowed
    assert "~shadow" in shadow.describe()
    plan = TradePlan(mode="seek_entry", triggers=[shadow])
    assert evaluate_plan(plan, _bar(1, 4001.0), position=0).action is Action.WAIT


def test_mark_feasibility_shadows_over_cap_trigger(cfg):
    cfg.risk.shadow_infeasible_triggers = True
    cfg.strategy.min_stop_atr_mult = 0.0   # disable the vol floor so stop_ticks are used as-is
    planner = Planner(cfg, _TwoTriggerAgent(cfg), synchronous=True)
    planner.schedule_plan_analysis(_preq(cfg))
    by_stop = {t.stop_ticks: t for t in planner.current_plan().triggers}
    assert by_stop[8].feasible is True
    assert by_stop[40].feasible is False
    assert "over_cap" in (by_stop[40].infeasible_reason or "")
    snap = planner.snapshot()
    assert snap["triggers_shadowed"] == 1
    assert "over_cap" in snap["shadow_reason"]


def test_shadow_flag_off_keeps_all_feasible(cfg):
    cfg.risk.shadow_infeasible_triggers = False            # the neutral default
    cfg.strategy.min_stop_atr_mult = 0.0
    planner = Planner(cfg, _TwoTriggerAgent(cfg), synchronous=True)
    planner.schedule_plan_analysis(_preq(cfg))
    assert all(t.feasible for t in planner.current_plan().triggers)
    assert planner.snapshot()["triggers_shadowed"] == 0
