"""Account-lifetime equity ledger — trailing-drawdown (MLL) math + persistence.

The ledger is the account-level primitive the RiskGate needs to enforce a prop firm's Max Loss
Limit (trailing drawdown) and to scale per-trade risk to the remaining room. It survives BOTH a
restart and a trading-day roll (unlike SessionState's day accounting). See the 2026-07-09 spec.
"""
from __future__ import annotations

from hermes_bridge.account_ledger import AccountLedger


def test_floor_starts_below_initial_ratchets_up_then_locks():
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    assert L.mll_floor() == 48000            # start: initial - MLL
    L.fold_day(1000)                          # EOD balance 51000
    assert L.eod_high_balance == 51000
    assert L.mll_floor() == 49000             # 51000 - 2000
    L.fold_day(1500)                          # EOD 52500 -> raw floor 50500, LOCKS at initial
    assert L.mll_floor() == 50000
    L.fold_day(-3000)                         # EOD 49500, but the high-water-mark never drops
    assert L.eod_high_balance == 52500
    assert L.mll_floor() == 50000             # locked, never moves down


def test_floor_none_when_mll_disabled():
    L = AccountLedger(initial_balance=50000, mll_amount=None)
    assert L.mll_floor() is None
    assert L.mll_room(unrealized=0.0) is None


def test_equity_and_room_include_prior_days_today_and_unrealized():
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    L.cumulative_realized = 500.0
    # equity = 50000 + 500 (prior) + 200 (today) + (-50) (open) = 50650
    assert L.equity(today_realized_net=200.0, unrealized=-50.0) == 50650
    # floor still 48000 (no EOD high past initial yet) -> room 2650
    assert L.mll_room(today_realized_net=200.0, unrealized=-50.0) == 2650


def test_room_goes_negative_on_breach():
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    # a live drawdown of 2100 puts equity 47900, below the 48000 floor
    assert L.mll_room(today_realized_net=-2100.0) == -100.0


def test_fold_ratchets_high_only_up():
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    L.fold_day(-500)                          # EOD 49500 < initial; high stays at initial
    assert L.eod_high_balance == 50000
    assert L.mll_floor() == 48000


def test_read_fresh_when_no_persistence(tmp_path):
    R = AccountLedger.read(str(tmp_path / "nope.json"), str(tmp_path / "nope.db"),
                           initial_balance=50000, mll_amount=2000)
    assert R.cumulative_realized == 0.0
    assert R.eod_high_balance == 50000
    assert R.mll_floor() == 48000


def test_persistence_roundtrip_json(tmp_path):
    jp = str(tmp_path / "acct.json")
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    L.fold_day(1200)                          # cumulative 1200, EOD high 51200
    L.write(json_path=jp, db_path=None)
    R = AccountLedger.read(jp, None, initial_balance=50000, mll_amount=2000)
    assert R.cumulative_realized == 1200
    assert R.eod_high_balance == 51200
    assert R.mll_floor() == 49200


def test_persistence_roundtrip_sqlite(tmp_path):
    dbp = str(tmp_path / "acct.db")
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    L.fold_day(800)                           # cumulative 800, EOD high 50800
    L.write(json_path=None, db_path=dbp)
    R = AccountLedger.read(None, dbp, initial_balance=50000, mll_amount=2000)
    assert R.cumulative_realized == 800
    assert R.eod_high_balance == 50800


def test_persist_writes_both_and_db_wins_on_read(tmp_path):
    jp = str(tmp_path / "acct.json")
    dbp = str(tmp_path / "acct.db")
    L = AccountLedger(initial_balance=50000, mll_amount=2000)
    L.fold_day(300)
    L.write(json_path=jp, db_path=dbp)
    R = AccountLedger.read(jp, dbp, initial_balance=50000, mll_amount=2000)
    assert R.cumulative_realized == 300


def test_account_switch_ignores_stale_persistence(tmp_path):
    # Persisted a 50k account; now a 100k account is selected -> stale history is dropped.
    jp = str(tmp_path / "acct.json")
    AccountLedger(initial_balance=50000, mll_amount=2000, cumulative_realized=1234).write(
        json_path=jp, db_path=None)
    R = AccountLedger.read(jp, None, initial_balance=100000, mll_amount=3000)
    assert R.initial_balance == 100000
    assert R.cumulative_realized == 0.0        # not carried across a different account
    assert R.eod_high_balance == 100000
    assert R.mll_amount == 3000                # current selection, not the persisted 2000


def test_mll_amount_follows_current_config_not_persisted(tmp_path):
    # Same account size, but the MLL number in config changed -> config wins, history kept.
    jp = str(tmp_path / "acct.json")
    AccountLedger(initial_balance=50000, mll_amount=2000, cumulative_realized=500).write(
        json_path=jp, db_path=None)
    R = AccountLedger.read(jp, None, initial_balance=50000, mll_amount=1800)
    assert R.cumulative_realized == 500        # same account -> history kept
    assert R.mll_amount == 1800                # config value wins
