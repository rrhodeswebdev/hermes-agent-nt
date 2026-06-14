from hermes_bridge.models import Action, OrderCommand
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState


def _session(cfg) -> SessionState:
    return SessionState(
        cfg.instrument.symbol, cfg.instrument.timeframe,
        cfg.instrument.tick_size, cfg.instrument.tick_value,
        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss,
    )


def _cmd(action, qty=1, stop_ticks=None, target_ticks=None, stop_price=None):
    return OrderCommand(
        id="c1", strategy_id="test-es", action=action, qty=qty,
        stop_ticks=stop_ticks, target_ticks=target_ticks, stop_price=stop_price,
    )


def test_exit_always_approved_even_when_halted(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    s.position = 2
    s.halt("daily_profit_target")
    rd = gate.evaluate(_cmd(Action.EXIT, qty=0), s)
    assert rd.approved
    assert rd.command.qty == 2  # filled in from position


def test_entry_rejected_when_halted(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    s.halt("max_daily_loss")
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s)
    assert not rd.approved
    assert any("halted" in r for r in rd.reasons)


def test_entry_rejected_when_in_position(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    s.position = 1
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s)
    assert not rd.approved
    assert "already_in_position" in rd.reasons


def test_entry_rejected_at_max_trades(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    s.trades_today = cfg.risk.max_trades_per_day
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s)
    assert not rd.approved
    assert "max_trades_per_day" in rd.reasons


def test_position_cap_and_risk_clamp(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    # stop 8 ticks → $100/contract; max_risk 250 → up to 2 by risk; cap also 2.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=5, stop_ticks=8), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.qty == 2
    assert any("qty_clamped_to_cap" in r for r in rd.reasons)


def test_default_stop_injected(cfg):
    cfg.risk.default_stop_ticks = 8  # keep risk under the cap so it can approve
    gate = RiskGate(cfg)
    s = _session(cfg)
    rd = gate.evaluate(_cmd(Action.ENTER_LONG), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.stop_ticks == 8
    assert any("default_stop_injected" in r for r in rd.reasons)


def test_single_contract_risk_exceeds_max_rejected(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    # 40 ticks * $12.50 = $500 > max_risk_per_trade 250
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=40), s)
    assert not rd.approved
    assert any("single_contract_risk_exceeds_max" in r for r in rd.reasons)


def test_daily_loss_projection_rejects(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    s.realized_pnl = -300.0  # already down; not yet halted (limit 400)
    # next trade risks $100; worst case -400 which hits the limit
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=8), s)
    assert not rd.approved
    assert any("would_breach_daily_loss" in r for r in rd.reasons)


def test_stop_price_distance_used(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    # entry ~4000, stop 4000-1.0 = 3999 → 4 ticks → $50 risk; approved with qty 1
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_price=3999.0), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.stop_price == 3999.0


def test_unsupported_action_rejected(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    # WAIT is not a real order; the engine never sends it, but the gate guards anyway.
    rd = gate.evaluate(_cmd(Action.WAIT), s)
    assert not rd.approved
    assert any("unsupported_action" in r for r in rd.reasons)


# --------------------------------------------------------------------------- #
# Risk rework: stop band + ATR-regime risk scaling                             #
# --------------------------------------------------------------------------- #
def test_stop_band_floor_widens_thin_tick_stop(cfg):
    cfg.strategy.min_stop_ticks = 20  # ES: 20t * $12.50 = $250 = the per-trade cap
    gate = RiskGate(cfg)
    s = _session(cfg)
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=2, stop_ticks=8), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.stop_ticks == 20                      # widened off the noise floor
    assert rd.command.qty == 1                              # wider stop → size clamps down
    assert any("stop_band_clamped:8->20" in r for r in rd.reasons)


def test_stop_band_ceiling_caps_wide_tick_stop(cfg):
    cfg.strategy.max_stop_ticks = 12
    cfg.risk.max_risk_per_trade = 1000.0  # don't let the dollar cap mask the tick ceiling
    gate = RiskGate(cfg)
    s = _session(cfg)
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=40), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.stop_ticks == 12
    assert any("stop_band_clamped:40->12" in r for r in rd.reasons)


def test_stop_band_floor_applies_to_price_stop(cfg):
    cfg.strategy.min_stop_ticks = 20  # 20 ticks * 0.25 = 5.0 points
    gate = RiskGate(cfg)
    s = _session(cfg)
    # A 1-point (4-tick) price stop is too tight → widened to a 5-point distance below entry.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_price=3999.0), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.stop_price == 3995.0
    assert any("stop_band_clamped" in r for r in rd.reasons)


def test_default_injected_stop_is_band_clamped(cfg):
    cfg.risk.default_stop_ticks = 8     # legacy thin default...
    cfg.strategy.min_stop_ticks = 16    # ...lifted to the floor (16t * $12.50 = $200 < $250)
    gate = RiskGate(cfg)
    s = _session(cfg)
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1), s, last_price=4000.0)
    assert rd.approved
    assert rd.command.stop_ticks == 16
    assert any("default_stop_injected" in r for r in rd.reasons)
    assert any("stop_band_clamped:8->16" in r for r in rd.reasons)


def test_risk_scale_shrinks_size_in_a_shock(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    # 8 ticks → $100/contract. Unscaled, $250 cap admits 2; halved budget ($125) admits 1.
    rd = gate.evaluate(
        _cmd(Action.ENTER_LONG, qty=2, stop_ticks=8), s, last_price=4000.0, risk_scale=0.5
    )
    assert rd.approved
    assert rd.command.qty == 1
    assert any("risk_scaled:0.5" in r for r in rd.reasons)


def test_risk_scale_can_reject_a_single_contract(cfg):
    gate = RiskGate(cfg)
    s = _session(cfg)
    # 8 ticks → $100; scaled budget 0.2*$250 = $50 < $100 → even one contract is too big.
    rd = gate.evaluate(
        _cmd(Action.ENTER_LONG, qty=1, stop_ticks=8), s, last_price=4000.0, risk_scale=0.2
    )
    assert not rd.approved
    assert any("single_contract_risk_exceeds_max" in r for r in rd.reasons)


# --------------------------------------------------------------------------- #
# Confidence-scaled position sizing                                            #
# --------------------------------------------------------------------------- #
def test_confidence_sizing_ramps_with_confidence(cfg):
    cfg.risk.confidence_sizing = True
    cfg.risk.max_contracts = 5
    cfg.risk.max_risk_per_trade = 5000.0    # don't let the per-trade dollar cap bind...
    cfg.daily_goal.max_daily_loss = 10_000.0  # ...nor the daily-loss projection — test the ramp
    cfg.strategy.min_confidence = 0.5
    cfg.risk.full_size_confidence = 0.9
    gate = RiskGate(cfg)
    s = _session(cfg)
    # 8 ticks * $12.50 = $100/contract; $5000 admits >5 so the cap (5) is the budget.
    # ramp: 0.5 → 1, midpoint 0.7 → 3, 0.9 → 5.
    lo = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s, last_price=4000.0, confidence=0.5)
    mid = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s, last_price=4000.0, confidence=0.7)
    hi = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s, last_price=4000.0, confidence=0.9)
    assert (lo.command.qty, mid.command.qty, hi.command.qty) == (1, 3, 5)
    assert any("confidence_sized" in r for r in hi.reasons)


def test_confidence_sizing_never_exceeds_dollar_budget(cfg):
    cfg.risk.confidence_sizing = True
    cfg.risk.max_contracts = 5              # cap allows 5...
    cfg.strategy.min_confidence = 0.5
    cfg.risk.full_size_confidence = 0.9
    gate = RiskGate(cfg)
    s = _session(cfg)
    # ...but 8t*$12.50 = $100/contract and the $250 cap admits only 2, so full confidence
    # still sizes to 2, not 5.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, stop_ticks=8), s, last_price=4000.0, confidence=1.0)
    assert rd.approved
    assert rd.command.qty == 2


def test_confidence_ignored_when_sizing_disabled(cfg):
    # Default cfg: confidence_sizing off → a qty=1 entry stays 1 even at full confidence
    # (the gate only clamps down; it never auto-sizes up). This is the legacy behavior.
    gate = RiskGate(cfg)
    s = _session(cfg)
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=8), s, last_price=4000.0,
                       confidence=1.0)
    assert rd.approved
    assert rd.command.qty == 1
    assert not any("confidence_sized" in r for r in rd.reasons)


# --------------------------------------------------------------------------- #
# Volatility-scaled stop floor (the "stops too close" fix)                     #
# --------------------------------------------------------------------------- #
def test_vol_floor_widens_a_too_tight_stop(cfg):
    cfg.strategy.min_stop_atr_mult = 1.5
    cfg.strategy.max_stop_ticks = 200          # ceiling well above the vol floor
    cfg.risk.max_risk_per_trade = 5000.0       # don't let the dollar cap reject the wide stop
    cfg.daily_goal.max_daily_loss = 10_000.0
    gate = RiskGate(cfg)
    s = _session(cfg)
    # ATR 8pts, tick 0.25 → vol floor = 1.5*8/0.25 = 48 ticks. A 4-tick stop is widened to it.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=4), s, last_price=4000.0, atr=8.0)
    assert rd.approved
    assert rd.command.stop_ticks == 48
    assert any("stop_band_clamped:4->48" in r for r in rd.reasons)


def test_vol_floor_still_capped_by_ceiling(cfg):
    cfg.strategy.min_stop_atr_mult = 2.0
    cfg.strategy.max_stop_ticks = 60           # ceiling BELOW the vol floor → ceiling wins
    cfg.risk.max_risk_per_trade = 5000.0
    cfg.daily_goal.max_daily_loss = 10_000.0
    gate = RiskGate(cfg)
    s = _session(cfg)
    # ATR 10pts → vol floor = 2.0*10/0.25 = 80 ticks, but the 60t ceiling caps it.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=4), s, last_price=4000.0, atr=10.0)
    assert rd.approved
    assert rd.command.stop_ticks == 60


def test_vol_floor_disabled_keeps_the_brain_stop(cfg):
    # Default cfg: min_stop_atr_mult=0 and no fixed band → a tight stop passes through even
    # with ATR supplied. (Proves the floor is opt-in and atr is otherwise inert.)
    gate = RiskGate(cfg)
    s = _session(cfg)
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=4), s, last_price=4000.0, atr=20.0)
    assert rd.approved
    assert rd.command.stop_ticks == 4


def test_vol_floor_applies_to_a_price_stop(cfg):
    cfg.strategy.min_stop_atr_mult = 1.5
    cfg.strategy.max_stop_ticks = 200
    cfg.risk.max_risk_per_trade = 5000.0
    cfg.daily_goal.max_daily_loss = 10_000.0
    gate = RiskGate(cfg)
    s = _session(cfg)
    # ATR 8pts → vol floor 48t = 12.0 pts. A 1-pt (4-tick) price stop is widened to 12 pts.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_price=3999.0), s, last_price=4000.0,
                       atr=8.0)
    assert rd.approved
    assert rd.command.stop_price == 3988.0


def test_target_widened_to_preserve_reward_risk(cfg):
    cfg.strategy.min_stop_atr_mult = 1.5
    cfg.strategy.atr_stop_mult = 2.0
    cfg.strategy.atr_target_mult = 3.0         # intended reward:risk = 3.0/2.0 = 1.5
    cfg.strategy.max_stop_ticks = 200
    cfg.risk.max_risk_per_trade = 5000.0
    cfg.daily_goal.max_daily_loss = 10_000.0
    gate = RiskGate(cfg)
    s = _session(cfg)
    # ATR 8 → stop widened 4t→48t. A tight 6t target would invert R:R; widen to 48*1.5 = 72t.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=4, target_ticks=6), s,
                       last_price=4000.0, atr=8.0)
    assert rd.approved
    assert rd.command.stop_ticks == 48
    assert rd.command.target_ticks == 72
    assert any("target_widened_to_rr" in r for r in rd.reasons)


def test_target_not_shrunk_when_already_wide(cfg):
    cfg.strategy.min_stop_atr_mult = 1.5
    cfg.strategy.atr_stop_mult = 2.0
    cfg.strategy.atr_target_mult = 3.0
    cfg.strategy.max_stop_ticks = 200
    cfg.risk.max_risk_per_trade = 5000.0
    cfg.daily_goal.max_daily_loss = 10_000.0
    gate = RiskGate(cfg)
    s = _session(cfg)
    # stop widened to 48t, but the brain's 150t target already exceeds the 72t floor → kept.
    rd = gate.evaluate(_cmd(Action.ENTER_LONG, qty=1, stop_ticks=4, target_ticks=150), s,
                       last_price=4000.0, atr=8.0)
    assert rd.command.target_ticks == 150
    assert not any("target_widened_to_rr" in r for r in rd.reasons)
