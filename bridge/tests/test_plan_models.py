from hermes_bridge.models import Action, Decision, OrderCommand, PlanSpec, Side


def test_plan_actions_exist():
    assert Action.ARM_PLAN == "ARM_PLAN"
    assert Action.CANCEL_ENTRY == "CANCEL_ENTRY"


def test_decision_carries_plan():
    d = Decision(action=Action.ARM_PLAN,
                 plan=PlanSpec(direction=Side.LONG, entry_low=1.0, entry_high=2.0,
                               ttl_bars=5, note="n"))
    assert d.plan.entry_high == 2.0
    assert Decision(action=Action.WAIT).plan is None


def test_order_command_limit_price_default_none():
    c = OrderCommand(id="x", strategy_id="s", action=Action.ENTER_LONG)
    assert c.limit_price is None
