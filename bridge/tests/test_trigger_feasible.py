"""The plan-time feasibility predicate MUST agree with the RiskGate bit-for-bit — it exists
so an un-fillable trigger is shadowed before arming, not silently rejected at fire time. If
the two ever drift, the filter would hide a fillable trigger (suppressing a real trade) or
arm an un-fillable one. The sweep below is the guarantee; it sits next to the gate's own tests
so a change to the cap/clamp math that breaks the mirror fails here immediately."""

import pytest

from hermes_bridge.models import Action, OrderCommand
from hermes_bridge.risk import evaluate_risk, per_contract_risk_usd, trigger_feasible
from hermes_bridge.session import SessionState


def _session(cfg) -> SessionState:
    return SessionState(
        cfg.instrument.symbol, cfg.instrument.timeframe,
        cfg.instrument.tick_size, cfg.instrument.tick_value,
        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss,
    )


def _enter(stop_ticks):
    return OrderCommand(
        id="c", strategy_id="t", action=Action.ENTER_LONG, qty=1,
        stop_ticks=stop_ticks, target_ticks=None, stop_price=None,
    )


# atr=None exercises the no-vol-floor path; 30/80 exercise the vol-floor widening (when the
# fixture enables min_stop_atr_mult) — the predicate must track the gate in every case.
@pytest.mark.parametrize("atr", [None, 30.0, 80.0])
@pytest.mark.parametrize("stop_ticks", list(range(2, 70)))
def test_predicate_matches_gate_across_stop_widths(cfg, stop_ticks, atr):
    # Flat, calm session so the ONLY thing that can reject across the sweep is single-contract
    # risk — exactly what the predicate checks. (Halt/position/news/hours all inactive.)
    gate = evaluate_risk(cfg, _enter(stop_ticks), _session(cfg), last_price=4000.0, atr=atr)
    feasible, reason = trigger_feasible(cfg, stop_ticks=stop_ticks, atr=atr)
    assert feasible == gate.approved, (stop_ticks, atr, reason, gate.reasons)
    if not feasible:
        assert reason is not None and "over_cap" in reason


def test_none_stop_is_feasible_via_injected_default(cfg):
    # A trigger with no stop is feasible: the gate injects default_stop_ticks (small).
    cfg.risk.default_stop_ticks = 8  # 8 * tick_value < cap
    feasible, reason = trigger_feasible(cfg, stop_ticks=None, atr=None)
    assert feasible and reason is None


def test_reason_carries_dollars_over_cap(cfg):
    # ES fixture: 40 ticks * $12.50 = $500 > $250 cap → infeasible, reason names both figures.
    feasible, reason = trigger_feasible(cfg, stop_ticks=40, atr=None)
    assert not feasible
    risk = per_contract_risk_usd(cfg, stop_ticks=40, atr=None)
    assert reason == f"over_cap(${risk:.0f}>${cfg.risk.max_risk_per_trade:.0f})"


def test_vol_shock_scale_tightens_feasibility(cfg):
    # A stop that fits at full budget can fail once the volatility-shock scale halves it.
    cfg.risk.max_risk_per_trade = 250.0
    # 16 ticks * $12.50 = $200 ≤ 250 (fits), but > 125 (half budget).
    assert trigger_feasible(cfg, stop_ticks=16, atr=None)[0] is True
    assert trigger_feasible(cfg, stop_ticks=16, atr=None, risk_scale=0.5)[0] is False
