"""Tests for commission tracking and net P&L.

TDD: these tests were written BEFORE the implementation to drive the feature.
All four scenarios must pass with commission_per_contract=0.0 unchanged (net==gross).
"""

import json

from hermes_bridge.config import ExecutionConfig
from hermes_bridge.models import Fill, Side
from hermes_bridge.session import SessionState


def _session(
    commission: float = 0.0, profit: float = 500.0, maxloss: float = 400.0
) -> SessionState:
    """ES: tick 0.25, $12.50/tick → $50 per 1.0 point per contract."""
    return SessionState("ES", "5m", 0.25, 12.5, profit, maxloss,
                        commission_per_contract=commission)


# ---- (a) 2-lot round-trip commission math -----------------------------------

def test_commission_2lot_roundtrip():
    """A 2-lot round-trip at $0.65/contract-side:
    entry fill: 2 contracts → 2 * 0.65 = $1.30
    exit fill:  2 contracts → 2 * 0.65 = $1.30
    total:                                $2.60
    """
    s = _session(commission=0.65)
    s.apply_fill(Fill(side=Side.LONG, qty=2, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.SHORT, qty=2, price=4004.0, ts=1))  # close
    assert s.position == 0
    # gross: 2 contracts * 4pts * $50 = $400
    assert round(s.realized_pnl, 2) == 400.0
    assert round(s.commission_total, 2) == 2.60
    assert round(s.realized_net, 2) == round(400.0 - 2.60, 2)


def test_commission_accrues_on_entry_and_exit():
    """Commission accrues on BOTH sides (entry and exit) separately."""
    s = _session(commission=0.65)
    # After entry: 1 lot → commission_total = 0.65
    s.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=0))
    assert round(s.commission_total, 2) == 0.65
    # After exit: another lot → commission_total = 1.30
    s.apply_fill(Fill(side=Side.SHORT, qty=1, price=4002.0, ts=1))
    assert round(s.commission_total, 2) == 1.30


def test_commission_zero_default_net_equals_gross():
    """With the neutral default (0.0) net P&L equals gross P&L."""
    s = _session(commission=0.0)
    s.apply_fill(Fill(side=Side.LONG, qty=2, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.SHORT, qty=2, price=4004.0, ts=1))
    assert round(s.commission_total, 2) == 0.0
    assert round(s.realized_net, 2) == round(s.realized_pnl, 2)


def test_commission_short_roundtrip():
    """Short round-trip also accrues commission on both sides."""
    s = _session(commission=0.65)
    s.apply_fill(Fill(side=Side.SHORT, qty=2, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.LONG, qty=2, price=3998.0, ts=1))
    assert round(s.commission_total, 2) == 2.60  # 2*0.65 + 2*0.65


# ---- (b) commission persists + restores across snapshot/restore -------------

def test_commission_persists_and_restores(tmp_path):
    """commission_total is written to the session snapshot and restored on a same-day restart."""
    sp = str(tmp_path / "session.json")
    ts = 1_781_500_000.0  # some trading day D

    s1 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp,
                      commission_per_contract=0.65)
    s1.maybe_roll_day(ts)
    s1.apply_fill(Fill(side=Side.LONG, qty=2, price=4000.0, ts=ts))
    s1.apply_fill(Fill(side=Side.SHORT, qty=2, price=4004.0, ts=ts))
    assert round(s1.commission_total, 2) == 2.60

    # Verify the JSON snapshot includes commission_total
    snap = json.loads((tmp_path / "session.json").read_text())
    assert "commission_total" in snap
    assert round(snap["commission_total"], 2) == 2.60

    # Restart same day: commission_total must be restored
    s2 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp,
                      commission_per_contract=0.65)
    s2.maybe_roll_day(ts + 300)  # same trading day
    assert round(s2.commission_total, 2) == 2.60
    assert round(s2.realized_net, 2) == round(s2.realized_pnl - 2.60, 2)


def test_commission_not_restored_on_new_day(tmp_path):
    """A new trading day starts with commission_total = 0 (old snapshot is not applied)."""
    sp = str(tmp_path / "session.json")
    ts = 1_781_500_000.0

    s1 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp,
                      commission_per_contract=0.65)
    s1.maybe_roll_day(ts)
    s1.apply_fill(Fill(side=Side.LONG, qty=2, price=4000.0, ts=ts))
    s1.apply_fill(Fill(side=Side.SHORT, qty=2, price=4004.0, ts=ts))

    # New trading day: commission resets to 0
    s2 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp,
                      commission_per_contract=0.65)
    s2.maybe_roll_day(ts + 86_400 * 2)  # 2 days later
    assert s2.commission_total == 0.0


def test_commission_restores_from_old_file_without_field(tmp_path):
    """Old session.json files without commission_total field default to 0.0 (backward compat)."""
    sp = tmp_path / "session.json"
    from hermes_bridge.indicators import cme_trading_day
    day = cme_trading_day(1_781_500_000.0)
    sp.write_text(json.dumps({
        "day": day,
        "realized_pnl": 200.0,
        "trades_today": 1,
        "halted": False,
        "halt_reason": "",
        "daily_goal_hit": False,
        # no commission_total key — simulating an old snapshot
    }))
    s = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=str(sp),
                     commission_per_contract=0.65)
    s.maybe_roll_day(1_781_500_000.0)
    assert round(s.realized_pnl, 2) == 200.0
    assert s.commission_total == 0.0  # safe default


# ---- (c) ExecutionConfig default is 0.0 ------------------------------------

def test_execution_config_default_commission():
    """ExecutionConfig.commission_per_contract defaults to 0.0 (neutral)."""
    cfg = ExecutionConfig()
    assert cfg.commission_per_contract == 0.0


def test_execution_config_commission_validation():
    """commission_per_contract must be >= 0."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ExecutionConfig(commission_per_contract=-0.01)


# ---- (d) account_state exposes realized_net + commission --------------------

def test_account_state_exposes_net_and_commission():
    """account_state() populates realized_net and commission fields."""
    s = _session(commission=0.65)
    s.apply_fill(Fill(side=Side.LONG, qty=2, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.SHORT, qty=2, price=4004.0, ts=1))

    acct = s.account_state()
    assert acct.commission == 2.60
    assert acct.realized_net == round(acct.realized_pnl - 2.60, 2)


def test_account_state_zero_commission_net_equals_gross():
    """With 0.0 commission, account_state.realized_net == realized_pnl."""
    s = _session(commission=0.0)
    s.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.SHORT, qty=1, price=4005.0, ts=1))

    acct = s.account_state()
    assert acct.commission == 0.0
    assert acct.realized_net == acct.realized_pnl
