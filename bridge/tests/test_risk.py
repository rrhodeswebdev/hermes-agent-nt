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
