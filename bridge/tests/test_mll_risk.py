"""RiskGate: trailing-drawdown (MLL) enforcement, dynamic room-scaled per-trade budget, and the
dynamic contract ceiling. Pure-function coverage of the 2026-07-09 spec's Stage 3.
"""
from __future__ import annotations

from hermes_bridge.config import BridgeConfig
from hermes_bridge.models import Action, OrderCommand
from hermes_bridge.risk import (
    effective_max_contracts,
    evaluate_risk,
    per_trade_budget,
)
from hermes_bridge.session import SessionState


def _base_cfg() -> BridgeConfig:
    cfg = BridgeConfig()
    cfg.instrument.tick_size = 0.25
    cfg.instrument.tick_value = 0.5
    return cfg


def _sess(cfg, *, ledger=True, mll=2000, size=50000, eval_pt=3000) -> SessionState:
    s = SessionState(
        cfg.instrument.symbol, cfg.instrument.timeframe,
        cfg.instrument.tick_size, cfg.instrument.tick_value,
        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss,
    )
    if ledger:
        s.attach_ledger(size, mll, eval_pt)
    return s


def _cmd(action, qty=1, stop_ticks=None, target_ticks=None):
    return OrderCommand(id="c1", strategy_id="t", action=action, qty=qty,
                        stop_ticks=stop_ticks, target_ticks=target_ticks)


# ---- per_trade_budget ---------------------------------------------------------
def test_budget_static_when_autoscale_off():
    cfg = _base_cfg()
    cfg.risk.max_risk_per_trade = 125
    assert per_trade_budget(cfg, _sess(cfg)) == 125     # ledger present but auto-scale off


def test_budget_static_when_no_ledger():
    cfg = _base_cfg()
    cfg.risk.auto_scale_per_trade = True
    cfg.risk.max_risk_per_trade = 125
    assert per_trade_budget(cfg, _sess(cfg, ledger=False)) == 125


def test_budget_scales_with_remaining_room():
    cfg = _base_cfg()
    cfg.risk.auto_scale_per_trade = True
    cfg.risk.per_trade_room_fraction = 0.15
    cfg.risk.per_trade_abs_ceiling_pct = 1.0     # don't let the equity ceiling bind
    cfg.risk.max_risk_per_trade = 100000         # don't let the backstop bind
    cfg.daily_goal.max_daily_loss = 100000       # don't let dll_room bind
    s = _sess(cfg)                               # floor 48000, equity 50000, room 2000
    assert per_trade_budget(cfg, s) == 300.0     # 0.15 * 2000


def test_budget_shrinks_near_floor_danger_band():
    cfg = _base_cfg()
    cfg.risk.auto_scale_per_trade = True
    cfg.risk.per_trade_room_fraction = 0.15
    cfg.risk.per_trade_abs_ceiling_pct = 1.0
    cfg.risk.max_risk_per_trade = 100000
    cfg.daily_goal.max_daily_loss = 100000
    s = _sess(cfg)
    s.realized_pnl = -1800.0                     # equity 48200, room 200
    assert per_trade_budget(cfg, s) == 30.0      # 0.15 * 200 -> tiny -> WAIT downstream


def test_budget_capped_by_daily_loss_room():
    cfg = _base_cfg()
    cfg.risk.auto_scale_per_trade = True
    cfg.risk.per_trade_room_fraction = 1.0
    cfg.risk.per_trade_abs_ceiling_pct = 1.0
    cfg.risk.max_risk_per_trade = 100000
    cfg.daily_goal.max_daily_loss = 300
    assert per_trade_budget(cfg, _sess(cfg)) == 300.0   # dll_room binds (300 + realized 0)


def test_budget_capped_by_static_backstop():
    cfg = _base_cfg()
    cfg.risk.auto_scale_per_trade = True
    cfg.risk.per_trade_room_fraction = 1.0
    cfg.risk.per_trade_abs_ceiling_pct = 1.0
    cfg.risk.max_risk_per_trade = 250
    cfg.daily_goal.max_daily_loss = 100000
    assert per_trade_budget(cfg, _sess(cfg)) == 250.0


# ---- effective_max_contracts --------------------------------------------------
def test_contracts_static_when_scaling_off():
    cfg = _base_cfg()
    cfg.risk.max_contracts = 40
    assert effective_max_contracts(cfg, _sess(cfg)) == 40


def test_contracts_full_in_eval_phase():
    # Lucid: NO scaling in the eval phase -> full size even with the flag on and tiers present.
    cfg = _base_cfg()
    cfg.risk.dynamic_contract_scaling = True
    cfg.risk.max_contracts = 40
    cfg.account_profile.phase = "eval"
    s = _sess(cfg)
    s.scaling_tiers = [(0, 20), (1000, 30), (2000, 40)]
    assert effective_max_contracts(cfg, s) == 40


def test_contracts_tiered_when_funded():
    cfg = _base_cfg()
    cfg.risk.dynamic_contract_scaling = True
    cfg.risk.max_contracts = 40
    cfg.account_profile.phase = "funded"
    s = _sess(cfg)
    s.scaling_tiers = [(0, 20), (1000, 30), (2000, 40)]   # real 50k LucidFlex funded tiers
    s.ledger.cumulative_realized = 0.0
    assert effective_max_contracts(cfg, s) == 20          # $0 profit -> lowest rung
    s.ledger.cumulative_realized = 1200.0
    assert effective_max_contracts(cfg, s) == 30          # $1,200 -> middle rung
    s.ledger.cumulative_realized = 5000.0
    assert effective_max_contracts(cfg, s) == 40          # past the top rung -> account max
    s.ledger.cumulative_realized = -500.0
    assert effective_max_contracts(cfg, s) == 20          # in drawdown -> smallest rung


def test_contracts_funded_without_tiers_uses_static():
    cfg = _base_cfg()
    cfg.risk.dynamic_contract_scaling = True
    cfg.risk.max_contracts = 40
    cfg.account_profile.phase = "funded"
    s = _sess(cfg)
    s.scaling_tiers = None
    assert effective_max_contracts(cfg, s) == 40


# ---- evaluate_risk integration ------------------------------------------------
def test_would_breach_mll_rejects_entry():
    cfg = _base_cfg()
    cfg.risk.enforce_trailing_drawdown = True
    cfg.daily_goal.max_daily_loss = 100000       # isolate the MLL check from the DLL check
    s = _sess(cfg)                               # floor 48000
    s.realized_pnl = -1999.0                      # equity 48001, room 1
    rd = evaluate_risk(cfg, _cmd(Action.ENTER_LONG, stop_ticks=40, target_ticks=80),
                       s, last_price=5000.0)
    assert not rd.approved
    assert any("would_breach_mll" in r for r in rd.reasons)


def test_mll_entry_allowed_with_room():
    cfg = _base_cfg()
    cfg.risk.enforce_trailing_drawdown = True
    s = _sess(cfg)                               # full room 2000
    rd = evaluate_risk(cfg, _cmd(Action.ENTER_LONG, stop_ticks=40, target_ticks=80),
                       s, last_price=5000.0)
    assert rd.approved


def test_dynamic_budget_waits_when_no_viable_contract():
    cfg = _base_cfg()
    cfg.risk.auto_scale_per_trade = True
    cfg.risk.per_trade_room_fraction = 0.15
    cfg.risk.per_trade_abs_ceiling_pct = 1.0
    cfg.risk.max_risk_per_trade = 100000
    cfg.daily_goal.max_daily_loss = 100000
    s = _sess(cfg)
    s.realized_pnl = -1900.0                      # room 100 -> budget 15; a 40-tick stop = $20/ct
    rd = evaluate_risk(cfg, _cmd(Action.ENTER_LONG, stop_ticks=40, target_ticks=80),
                       s, last_price=5000.0)
    assert not rd.approved
    assert any("single_contract_risk_exceeds_max" in r for r in rd.reasons)
