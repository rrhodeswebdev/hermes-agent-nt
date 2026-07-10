"""SessionState <-> AccountLedger integration: fold-on-day-roll, MLL halt, equity accessors,
and persistence of the ledger sidecar next to the day-state file. See the 2026-07-09 spec.
"""
from __future__ import annotations

from hermes_bridge.session import SessionState

DAY1 = 1_781_000_000.0
DAY_LATER = DAY1 + 3 * 86_400  # a different CME trading day


def _sess(tmp_path):
    return SessionState(
        "MNQ", "2m", 0.25, 0.5,
        profit_target=1500.0, max_daily_loss=1200.0,
        state_path=str(tmp_path / "session.json"),
        commission_per_contract=0.65,
        ledger_db_path=None,
    )


def test_fold_on_day_roll_banks_net_and_ratchets_high(tmp_path):
    s = _sess(tmp_path)
    s.attach_ledger(50000, 2000, 3000)
    s.maybe_roll_day(DAY1)              # establish day 1 (no fold)
    s.realized_pnl = 900.0
    s.commission_total = 100.0         # net = 800
    assert s.maybe_roll_day(DAY_LATER) is True
    assert s.realized_pnl == 0.0                    # day accounting reset
    assert s.ledger.cumulative_realized == 800.0    # NET folded into the lifetime total
    assert s.ledger.eod_high_balance == 50800.0
    assert s.mll_floor() == 48800.0                 # min(50800 - 2000, 50000)


def test_check_mll_halts_on_breach_and_is_idempotent(tmp_path):
    s = _sess(tmp_path)
    s.attach_ledger(50000, 2000, 3000)              # floor 48000
    s.realized_pnl = -2000.0
    s.commission_total = 100.0                      # net -2100 -> equity 47900 < 48000
    assert s.check_mll(mark_price=None) == "mll_breached"
    assert s.halted
    assert s.check_mll(mark_price=None) is None      # idempotent once halted


def test_check_mll_respects_buffer(tmp_path):
    s = _sess(tmp_path)
    s.attach_ledger(50000, 2000, 3000)              # floor 48000
    s.realized_pnl = -1850.0                         # equity 48150, room 150
    assert s.check_mll(mark_price=None, buffer_usd=0.0) is None
    assert s.check_mll(mark_price=None, buffer_usd=200.0) == "mll_breached"  # 150 <= 200


def test_check_mll_ok_with_room(tmp_path):
    s = _sess(tmp_path)
    s.attach_ledger(50000, 2000, 3000)
    s.realized_pnl = -500.0
    assert s.check_mll(mark_price=None) is None
    assert not s.halted


def test_accessors_none_without_ledger(tmp_path):
    s = _sess(tmp_path)
    assert s.mll_floor() is None
    assert s.account_equity(100.0) is None
    assert s.mll_room() is None
    assert s.check_mll(100.0) is None


def test_account_equity_includes_unrealized(tmp_path):
    s = _sess(tmp_path)
    s.attach_ledger(50000, 2000, 3000)
    s.position = 2
    s.avg_price = 100.0
    # point_value = tick_value/tick_size = 0.5/0.25 = 2; unreal at 105 = (105-100)*2*2 = 20
    assert s.account_equity(mark_price=105.0) == 50020.0
    assert s.mll_room(mark_price=105.0) == 2020.0    # 50020 - 48000


def test_ledger_persists_next_to_session_state(tmp_path):
    s = _sess(tmp_path)
    s.attach_ledger(50000, 2000, 3000)
    s.maybe_roll_day(DAY1)
    s.realized_pnl = 1200.0
    s.maybe_roll_day(DAY_LATER)                      # folds net 1200
    # A fresh SessionState with the same state_path restores the ledger from the sidecar.
    s2 = _sess(tmp_path)
    s2.attach_ledger(50000, 2000, 3000)
    assert s2.ledger.cumulative_realized == 1200.0
    assert s2.ledger.eod_high_balance == 51200.0
