from hermes_bridge.models import Fill, Side
from hermes_bridge.session import SessionState


def _session(profit=500.0, maxloss=400.0) -> SessionState:
    # ES: tick 0.25, $12.50/tick → $50 per 1.0 point per contract.
    return SessionState("ES", "5m", 0.25, 12.5, profit, maxloss)


def test_long_roundtrip_profit():
    s = _session()
    s.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=0))
    assert s.position == 1
    assert s.trades_today == 1
    s.apply_fill(Fill(side=Side.SHORT, qty=1, price=4004.0, ts=1))  # sell to close
    assert s.position == 0
    # 4 points * $50 = $200
    assert round(s.realized_pnl, 2) == 200.0


def test_short_roundtrip_profit():
    s = _session()
    s.apply_fill(Fill(side=Side.SHORT, qty=2, price=4000.0, ts=0))
    assert s.position == -2
    s.apply_fill(Fill(side=Side.LONG, qty=2, price=3998.0, ts=1))  # buy to cover lower
    assert s.position == 0
    # 2 points * $50 * 2 contracts = $200
    assert round(s.realized_pnl, 2) == 200.0


def test_partial_close_then_flip():
    s = _session()
    s.apply_fill(Fill(side=Side.LONG, qty=2, price=4000.0, ts=0))   # long 2 @4000
    s.apply_fill(Fill(side=Side.SHORT, qty=3, price=4002.0, ts=1))  # sell 3 → flip to short 1
    assert s.position == -1
    # Closed 2 longs for +2pts*$50*2 = $200; remaining short opened @4002
    assert round(s.realized_pnl, 2) == 200.0
    assert s.avg_price == 4002.0


def test_daily_profit_target_halts():
    s = _session(profit=150.0)
    s.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.SHORT, qty=1, price=4004.0, ts=1))  # +$200 > 150
    assert s.check_daily_goal() == "daily_profit_target"
    assert s.halted and s.daily_goal_hit


def test_max_daily_loss_halts():
    s = _session(maxloss=100.0)
    s.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=0))
    s.apply_fill(Fill(side=Side.SHORT, qty=1, price=3996.0, ts=1))  # -$200 < -100
    assert s.check_daily_goal() == "max_daily_loss"
    assert s.halted


def test_day_roll_resets():
    s = _session()
    s.maybe_roll_day(0)
    s.realized_pnl = 123.0
    s.trades_today = 3
    s.halt("x")
    rolled = s.maybe_roll_day(86400 * 2)  # two days later
    assert rolled
    assert s.realized_pnl == 0.0 and s.trades_today == 0 and not s.halted


def test_unrealized_pnl():
    s = _session()
    s.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=0))
    assert s.unrealized_pnl(4002.0) == 100.0  # 2 pts * $50
    assert s.unrealized_pnl(3999.0) == -50.0


def test_session_state_persists_and_restores_same_day(tmp_path):
    """A mid-day restart restores realized P&L + trade count from disk; a new day starts
    clean. Position is never persisted (a clean restart is flat)."""
    sp = str(tmp_path / "session.json")
    ts = 1_781_500_000.0  # some trading day D
    s1 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp)
    s1.maybe_roll_day(ts)
    s1.apply_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=ts))
    s1.apply_fill(Fill(side=Side.SHORT, qty=1, price=4004.0, ts=ts))  # +$200, 1 trade
    assert s1.position == 0 and s1.trades_today == 1
    assert round(s1.realized_pnl, 2) == 200.0

    # Restart same day: a fresh session over the same file restores on the first bar.
    s2 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp)
    assert s2.realized_pnl == 0.0 and s2.trades_today == 0  # not applied until a bar arrives
    s2.maybe_roll_day(ts + 300)  # same UTC day -> restore
    assert round(s2.realized_pnl, 2) == 200.0
    assert s2.trades_today == 1

    # A NEW trading day starts clean (never carry yesterday's P&L forward).
    s3 = SessionState("ES", "5m", 0.25, 12.5, 500, 400, state_path=sp)
    s3.maybe_roll_day(ts + 86_400 * 2)
    assert s3.realized_pnl == 0.0 and s3.trades_today == 0
