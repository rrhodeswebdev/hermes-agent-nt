"""The armed exit levels must REST in NinjaTrader, not be tested once per bar close.

Both discretionary exits (the plan's ExitRule and the managed breakeven/trail stop) were
close-tests: the bridge only sees completed bars, so a fast bar could run arbitrarily far
past the armed level before the exit could fire. Observed 2026-08-04: an exit armed at
29817 filled at 29832 (62 ticks of overshoot) while the resting NT8 bracket stop the same
night filled within 0.75 ticks of its price.
"""

from hermes_bridge.agent_client import MockAgentClient
from hermes_bridge.config import (
    BridgeConfig,
    DailyGoal,
    InstrumentConfig,
    RiskParams,
    StrategyParams,
)
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.models import Action, Decision, OrderCommand, Side
from hermes_bridge.plan import ExitRule, Planner, TradePlan
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.stops import plan_exit_stop_price, tightest_stop
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, make_session


def _session(cfg) -> SessionState:
    return SessionState(
        cfg.instrument.symbol, cfg.instrument.timeframe,
        cfg.instrument.tick_size, cfg.instrument.tick_value,
        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss,
    )


class _HoldAgent(MockAgentClient):
    def decide(self, req):
        return Decision(action=Action.WAIT, rationale="hold")


def _amend_cfg(**strat) -> BridgeConfig:
    base = dict(atr_period=14, swing_lookback=3, min_confidence=0.55)
    base.update(strat)
    return BridgeConfig(
        strategy_id="test-es",
        instrument=InstrumentConfig(symbol="ES", timeframe="5m", tick_size=0.25, tick_value=12.5),
        strategy=StrategyParams(**base),
        risk=RiskParams(max_contracts=2, max_risk_per_trade=250.0, default_stop_ticks=16),
        daily_goal=DailyGoal(profit_target=500.0, max_daily_loss=400.0),
    )


def _engine_long(cfg, entry: float, stop_ticks: int = 8):
    """An engine holding an open long at `entry`, seeded flat so ATR > 0."""
    store = BarStore("ES", "5m")
    seed = [
        make_bar(1_700_000_000 + i * 300, m, m + 1.0, m - 1.0, m)
        for i, m in enumerate(entry + (0.75 if i % 2 else -0.75) for i in range(80))
    ]
    store.replace_history(seed)
    session = make_session(cfg)
    session.position = 1
    session.avg_price = entry
    # The bracket NinjaTrader is already resting for this trade, 20pts under entry. Every
    # amendment ratchets against it, so it is what stops a looser level being sent.
    session.working_stop = entry - 20.0
    engine = TradingEngine(cfg, store, session, _HoldAgent(cfg), RiskGate(cfg))
    engine._active_stop_ticks = stop_ticks
    ctx = build_context(seed, atr_period=cfg.strategy.atr_period,
                        swing_lookback=cfg.strategy.swing_lookback)
    engine.tracker.on_entry(ts=seed[-1].ts, side=Side.LONG, qty=1, price=entry,
                            context=ctx, rationale="entry")
    return engine, ctx, seed[-1]


def _manage_plan(exit_below: float) -> TradePlan:
    return TradePlan(mode="manage_position", exit=ExitRule(exit_below=exit_below,
                                                           rationale="invalidation"))


# ---- plan_exit_stop_price: the resting level for an armed plan exit --------------------

def test_short_plan_exit_rests_a_stop_buffer_ticks_above_the_armed_level():
    # SHORT armed "exit if close >= 29817"; a 32-tick (8pt) buffer rests the stop at 29825,
    # so the worst case is -8pt instead of however far the bar ran.
    assert plan_exit_stop_price(
        side="SHORT", exit_below=None, exit_above=29817.0,
        buffer_ticks=32, tick_size=0.25,
    ) == 29825.0


def test_long_plan_exit_rests_a_stop_buffer_ticks_below_the_armed_level():
    assert plan_exit_stop_price(
        side="LONG", exit_below=29900.0, exit_above=None,
        buffer_ticks=32, tick_size=0.25,
    ) == 29892.0


def test_zero_buffer_rests_the_stop_exactly_on_the_armed_level():
    assert plan_exit_stop_price(
        side="SHORT", exit_below=None, exit_above=29817.0,
        buffer_ticks=0, tick_size=0.25,
    ) == 29817.0


def test_no_armed_level_on_the_relevant_side_rests_nothing():
    # A long's invalidation is exit_below; an exit_above alone says nothing about its stop.
    assert plan_exit_stop_price(
        side="LONG", exit_below=None, exit_above=29817.0,
        buffer_ticks=32, tick_size=0.25,
    ) is None


# ---- tightest_stop: pick the most protective of several candidate levels ---------------

def test_tightest_stop_for_a_long_is_the_highest_level():
    assert tightest_stop("LONG", [29800.0, 29850.0, 29820.0]) == 29850.0


def test_tightest_stop_for_a_short_is_the_lowest_level():
    assert tightest_stop("SHORT", [29900.0, 29850.0, 29880.0]) == 29850.0


def test_tightest_stop_ignores_missing_levels():
    assert tightest_stop("LONG", [None, 29850.0, None]) == 29850.0


def test_tightest_stop_of_nothing_is_none():
    assert tightest_stop("LONG", [None, None]) is None


# ---- the RiskGate is the authority for moving a working stop --------------------------

def test_amend_stop_is_approved_when_it_tightens_a_long(cfg):
    s = _session(cfg)
    s.position = 2
    s.avg_price = 29900.0
    s.working_stop = 29880.0
    rd = RiskGate(cfg).evaluate(
        OrderCommand(id="a1", strategy_id="t", action=Action.AMEND_STOP,
                     stop_price=29892.0),
        s, last_price=29910.0,
    )
    assert rd.approved
    assert rd.command.stop_price == 29892.0


def test_amend_stop_is_rejected_when_it_would_widen_risk(cfg):
    # A stop that moves AWAY from price increases exposure — the gate must never allow it.
    s = _session(cfg)
    s.position = 2
    s.avg_price = 29900.0
    s.working_stop = 29892.0
    rd = RiskGate(cfg).evaluate(
        OrderCommand(id="a2", strategy_id="t", action=Action.AMEND_STOP,
                     stop_price=29880.0),
        s, last_price=29910.0,
    )
    assert not rd.approved
    assert any("stop_not_tighter" in r for r in rd.reasons)


def test_amend_stop_is_rejected_when_flat(cfg):
    s = _session(cfg)
    rd = RiskGate(cfg).evaluate(
        OrderCommand(id="a3", strategy_id="t", action=Action.AMEND_STOP,
                     stop_price=29892.0),
        s, last_price=29910.0,
    )
    assert not rd.approved
    assert any("no_position" in r for r in rd.reasons)


def test_amend_stop_is_rejected_when_already_through_price(cfg):
    # A long's stop at/above the last price would fill instantly at market — that is an
    # EXIT, and it must go through the exit path, not be disguised as a stop amendment.
    s = _session(cfg)
    s.position = 2
    s.avg_price = 29900.0
    s.working_stop = 29880.0
    rd = RiskGate(cfg).evaluate(
        OrderCommand(id="a4", strategy_id="t", action=Action.AMEND_STOP,
                     stop_price=29915.0),
        s, last_price=29910.0,
    )
    assert not rd.approved
    assert any("stop_through_price" in r for r in rd.reasons)


def test_amend_stop_tightens_a_short_downward(cfg):
    s = _session(cfg)
    s.position = -2
    s.avg_price = 29900.0
    s.working_stop = 29940.0
    rd = RiskGate(cfg).evaluate(
        OrderCommand(id="a5", strategy_id="t", action=Action.AMEND_STOP,
                     stop_price=29925.0),
        s, last_price=29890.0,
    )
    assert rd.approved
    assert rd.command.stop_price == 29925.0


def test_working_stop_clears_when_the_position_closes(cfg):
    from hermes_bridge.models import Fill
    s = _session(cfg)
    s.position = 1
    s.avg_price = 5000.0
    s.working_stop = 4990.0
    s.apply_fill(Fill(side=Side.SHORT, qty=1, price=5010.0, ts=1.0, position_after=0))
    assert s.position == 0
    assert s.working_stop is None


# ---- the engine turns an armed level into a resting stop ------------------------------

def test_engine_amends_the_stop_to_the_buffered_plan_exit_level():
    # Armed "exit if close <= 4990" with a 32-tick (8pt) buffer ⇒ a real stop rests at 4982,
    # so a fast bar can lose at most 8pts past the armed level instead of the whole bar range.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    cmd = engine._stop_amendment(ctx, bar, _manage_plan(4990.0))
    assert cmd is not None
    assert cmd.action == Action.AMEND_STOP
    assert cmd.stop_price == 4982.0


def test_engine_amends_nothing_when_the_buffer_is_disabled():
    # 0 = off is the committed default: behaviour is exactly as it was before this feature.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=0, breakeven_r=0.0)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    assert engine._stop_amendment(ctx, bar, _manage_plan(4990.0)) is None


def test_engine_does_not_re_amend_a_level_already_resting():
    # The ratchet: re-sending the same level every bar would spam NinjaTrader with no-ops.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    first = engine._stop_amendment(ctx, bar, _manage_plan(4990.0))
    assert first is not None
    assert engine.session.working_stop == 4982.0
    assert engine._stop_amendment(ctx, bar, _manage_plan(4990.0)) is None


def test_engine_amends_only_when_the_new_level_is_tighter():
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    engine._stop_amendment(ctx, bar, _manage_plan(4990.0))     # rests at 4982
    # The brain loosens its invalidation — the resting stop must NOT follow it down.
    assert engine._stop_amendment(ctx, bar, _manage_plan(4970.0)) is None
    assert engine.session.working_stop == 4982.0
    # ...but a tighter one is taken.
    tighter = engine._stop_amendment(ctx, bar, _manage_plan(4995.0))
    assert tighter is not None
    assert engine.session.working_stop == 4987.0


def test_engine_rests_the_tighter_of_managed_stop_and_plan_exit():
    # +1R reached ⇒ managed stop is at breakeven (5000); the plan exit buffers to 4982.
    # The managed level is tighter for a long, so that is what rests.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32, breakeven_r=1.0, trail_enabled=False)
    engine, ctx, bar = _engine_long(cfg, 5000.0, stop_ticks=8)
    engine.tracker.on_bar(make_bar(bar.ts + 300, 5002.0, 5004.0, 5001.0, 5003.0))
    cmd = engine._stop_amendment(ctx, bar, _manage_plan(4990.0))
    assert cmd is not None
    assert cmd.stop_price == 5000.0


def test_engine_never_widens_the_stop_already_resting():
    # The armed invalidation buffers to 4962 — LOOSER than the 4980 bracket already in the
    # market. Moving a live stop away from price is the one thing this must never do.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    assert engine._stop_amendment(ctx, bar, _manage_plan(4970.0)) is None
    assert engine.session.working_stop == 4980.0


def test_engine_proposes_nothing_when_no_resting_stop_is_known():
    # Without a baseline there is no proof an amendment tightens, so it could silently
    # widen NinjaTrader's stop. Fail closed: propose nothing.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    engine.session.working_stop = None
    assert engine._stop_amendment(ctx, bar, _manage_plan(4990.0)) is None


def test_entry_fill_seeds_the_working_stop_from_its_bracket():
    # The seed is what makes the ratchet meaningful: without it the first amendment has
    # nothing to prove it tightens against, and could move NinjaTrader's stop wider.
    from hermes_bridge.models import Fill
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, ctx, bar = _engine_long(cfg, 5000.0)
    engine.session.position = 0            # rewind to flat, entry approved and in flight
    engine.session.avg_price = 0.0
    engine.session.working_stop = None
    engine._pending_entry = {
        "cmd_id": "e1", "ts": bar.ts, "side": Side.LONG, "context": ctx,
        "rationale": "entry", "confidence": 0.6,
        "command": OrderCommand(id="e1", strategy_id="test-es", action=Action.ENTER_LONG,
                                qty=1, stop_price=4980.0, target_price=5040.0),
        "stop_ticks": 80, "brackets": (4980.0, 5040.0),
    }
    engine.on_fill(Fill(order_id="e1", side=Side.LONG, qty=1, price=5000.0,
                        ts=bar.ts + 1, position_after=1))
    assert engine.session.working_stop == 4980.0


def test_engine_amendment_is_surfaced_on_the_bar_result():
    # End-to-end through the real plan cycle: an armed manage_position plan whose exit does
    # NOT fire this bar still rests its buffered stop.
    cfg = _amend_cfg(plan_exit_stop_buffer_ticks=32)
    engine, _ctx, bar = _engine_long(cfg, 5000.0)
    planner = Planner(cfg, MockAgentClient(cfg), synchronous=True)
    engine.planner = planner
    next_bar = make_bar(bar.ts + 300, 5000.0, 5001.0, 4999.0, 5000.0)
    planner.arm(TradePlan(mode="manage_position", based_on_bar_ts=next_bar.ts,
                          exit=ExitRule(exit_below=4990.0, rationale="invalidation")))
    result = engine.on_bar(next_bar)
    assert result.decision.action == Action.WAIT          # the exit did NOT fire
    assert [c.action for c in result.extra_commands] == [Action.AMEND_STOP]
    assert result.extra_commands[0].stop_price == 4982.0
