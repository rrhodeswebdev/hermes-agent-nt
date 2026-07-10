"""SessionState <-> AccountLedger integration: fold-on-day-roll, MLL halt, equity accessors,
and persistence of the ledger sidecar next to the day-state file. See the 2026-07-09 spec.
"""
from __future__ import annotations

import json

from hermes_bridge.indicators import cme_trading_day
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


def _shutdown_with_unfolded_day(tmp_path):
    """Session 1 trades DAY1 and is shut down BEFORE its day roll (as over a weekend): the day's
    net lives in session.json but was never folded into the ledger sidecar (still cumulative 0)."""
    s1 = _sess(tmp_path)
    s1.attach_ledger(50000, 2000, 3000)
    s1.maybe_roll_day(DAY1)
    s1.realized_pnl = 300.0
    s1.commission_total = 20.0            # net 280
    s1._persist()                          # session.json = {day DAY1, realized 300, comm 20}
    assert s1.ledger.cumulative_realized == 0.0   # never folded (no live roll happened)
    return s1


def test_boot_fold_banks_prior_day_when_down_across_roll(tmp_path):
    _shutdown_with_unfolded_day(tmp_path)
    # Fresh process; the first live bar is a LATER trading day (the bridge was down across the
    # roll). The prior day's net must be banked into the ledger now, not silently dropped.
    s2 = _sess(tmp_path)
    s2.attach_ledger(50000, 2000, 3000)
    assert s2.ledger.cumulative_realized == 0.0
    s2.maybe_roll_day(DAY_LATER)
    assert s2.ledger.cumulative_realized == 280.0            # DAY1 net folded on boot
    assert s2.ledger.eod_high_balance == 50280.0            # high-water-mark ratcheted
    assert s2.mll_floor() == 48280.0                         # min(50280 - 2000, 50000)
    assert s2.realized_pnl == 0.0                            # the new day starts clean
    assert s2.ledger.last_folded_day == cme_trading_day(DAY1)


def _write_day_state(tmp_path, day_value, realized, commission):
    """Overwrite session.json directly (no ledger interaction) — simulates a lost day-reset
    persist that left an already-CLOSED day's accounting on disk."""
    (tmp_path / "session.json").write_text(json.dumps({
        "day": day_value, "realized_pnl": realized, "commission_total": commission,
        "trades_today": 1, "halted": False, "halt_reason": "", "daily_goal_hit": False,
    }), encoding="utf-8")


def test_boot_fold_not_double_counted_on_second_restart(tmp_path):
    _shutdown_with_unfolded_day(tmp_path)
    s2 = _sess(tmp_path)
    s2.attach_ledger(50000, 2000, 3000)
    s2.maybe_roll_day(DAY_LATER)                              # folds 280 once
    assert s2.ledger.cumulative_realized == 280.0
    # Simulate the day-reset persist having been lost (raced write): session.json still shows the
    # already-banked DAY1. A second restart must NOT re-bank it — the ledger's last_folded_day
    # (restored from its sidecar) refuses the duplicate fold.
    _write_day_state(tmp_path, cme_trading_day(DAY1), 300.0, 20.0)
    s3 = _sess(tmp_path)
    s3.attach_ledger(50000, 2000, 3000)
    assert s3.ledger.last_folded_day == cme_trading_day(DAY1)
    s3.maybe_roll_day(DAY_LATER)
    assert s3.ledger.cumulative_realized == 280.0            # still 280, NOT 560


def test_same_day_restart_restores_and_does_not_fold(tmp_path):
    _shutdown_with_unfolded_day(tmp_path)
    # A mid-day restart (SAME trading day) restores the day's P&L into the session and folds
    # nothing — the day is not closed yet.
    s2 = _sess(tmp_path)
    s2.attach_ledger(50000, 2000, 3000)
    s2.maybe_roll_day(DAY1)
    assert s2.realized_pnl == 300.0
    assert s2.commission_total == 20.0
    assert s2.ledger.cumulative_realized == 0.0
    assert s2.ledger.last_folded_day is None
