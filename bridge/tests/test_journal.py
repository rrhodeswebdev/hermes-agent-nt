from hermes_bridge.indicators import build_context
from hermes_bridge.journal import ClosedTrade, JournalStore, TradeTracker, select_similar
from hermes_bridge.models import Bar, Side
from tests.conftest import synthetic_bars


def _trade(**kw):
    base = dict(entry_ts=1.0, exit_ts=2.0, side="LONG", qty=1, entry_price=100.0,
                exit_price=102.0, realized_pnl=2.0, bars_held=3, mae=-0.5, mfe=2.5,
                trend="up", entry_context={"trend": "up"}, rationale="r")
    base.update(kw)
    return ClosedTrade(**base)


def test_journal_append_and_recent(tmp_path):
    path = tmp_path / "state" / "journal.jsonl"
    js = JournalStore(str(path))
    js.append(_trade(realized_pnl=1.0))
    js.append(_trade(realized_pnl=2.0))
    js.append(_trade(realized_pnl=3.0))
    recent = js.recent(2)
    assert len(recent) == 2
    assert recent[-1]["realized_pnl"] == 3.0
    assert recent[0]["realized_pnl"] == 2.0  # most-recent-last ordering


def test_journal_recent_on_missing_file(tmp_path):
    js = JournalStore(str(tmp_path / "nope.jsonl"))
    assert js.recent(5) == []


def _ctx(trend_bars):
    return build_context(trend_bars, atr_period=14)


def test_tracker_long_lifecycle_mae_mfe():
    bars = synthetic_bars(60)
    ctx = _ctx(bars)
    t = TradeTracker()
    t.on_entry(ts=1.0, side=Side.LONG, qty=1, price=100.0, context=ctx, rationale="long")
    t.on_bar(Bar(ts=2.0, open=100, high=105, low=98, close=104))   # fav +5, adv -2
    t.on_bar(Bar(ts=3.0, open=104, high=103, low=96, close=99))    # adv -4 (new max adverse)
    trade = t.on_exit(ts=4.0, price=101.0, realized_pnl=1.0)
    assert trade is not None
    assert trade.side == "LONG"
    assert trade.bars_held == 2
    assert trade.mfe == 5.0
    assert trade.mae == -4.0
    assert trade.realized_pnl == 1.0


def test_tracker_exit_without_entry_returns_none():
    assert TradeTracker().on_exit(ts=1.0, price=1.0, realized_pnl=0.0) is None


def test_select_similar_prefers_same_trend():
    trades = [{"trend": "down", "realized_pnl": -1}, {"trend": "up", "realized_pnl": 1},
              {"trend": "up", "realized_pnl": 2}]
    bars = synthetic_bars(60)  # synthetic data trends up
    ctx = _ctx(bars)
    assert ctx.trend == "up"
    out = select_similar(trades, ctx, 2)
    assert all(t["trend"] == "up" for t in out)
    assert out[-1]["realized_pnl"] == 2


def test_tracker_records_entry_confidence():
    bars = synthetic_bars(60)
    ctx = _ctx(bars)
    t = TradeTracker()
    t.on_entry(ts=1.0, side=Side.LONG, qty=1, price=100.0, context=ctx,
               rationale="long", confidence=0.73)
    t.on_bar(Bar(ts=2.0, open=100, high=105, low=98, close=104))
    trade = t.on_exit(ts=3.0, price=101.0, realized_pnl=1.0)
    assert trade is not None
    assert trade.confidence == 0.73
    assert trade.to_record()["confidence"] == 0.73


def test_tracker_confidence_defaults_zero_when_absent():
    bars = synthetic_bars(60)
    ctx = _ctx(bars)
    t = TradeTracker()
    t.on_entry(ts=1.0, side=Side.SHORT, qty=1, price=100.0, context=ctx, rationale="s")
    trade = t.on_exit(ts=2.0, price=99.0, realized_pnl=1.0)
    assert trade is not None
    assert trade.confidence == 0.0
    assert "confidence" in trade.to_record()


# ---- RiskGate reasons are journaled -----------------------------------------
def test_risk_reasons_default_to_empty():
    assert _trade().risk_reasons == []
    assert _trade().to_record()["risk_reasons"] == []


def test_tracker_carries_risk_reasons_onto_the_closed_trade():
    # sizing_conf_capped only ever occurs on an APPROVED order, so it never reaches the
    # decline log. Without this the learning loop cannot tell a capped trade from any other.
    reasons = ["confidence_sized:0.68->3", "sizing_conf_capped:0.68->0.62"]
    t = TradeTracker()
    ctx = build_context(synthetic_bars(60), atr_period=14)
    t.on_entry(ts=1.0, side=Side.LONG, qty=3, price=100.0, context=ctx,
               rationale="r", risk_reasons=reasons)
    trade = t.on_exit(ts=2.0, price=101.0, realized_pnl=6.0)
    assert trade.risk_reasons == reasons
    assert trade.to_record()["risk_reasons"] == reasons


# ---- the exit fill counts toward the excursion ------------------------------
def _tracked(side, entry, bar_hi, bar_lo, exit_px):
    """Open a trade, show it ONE bar, then exit at exit_px."""
    t = TradeTracker()
    ctx = build_context(synthetic_bars(60), atr_period=14)
    t.on_entry(ts=1.0, side=side, qty=1, price=entry, context=ctx, rationale="r")
    t.on_bar(Bar(ts=2.0, open=entry, high=bar_hi, low=bar_lo, close=entry, volume=1.0))
    return t.on_exit(ts=3.0, price=exit_px, realized_pnl=0.0)


def test_mfe_includes_the_exit_fill_for_a_long():
    # engine.on_bar only feeds the tracker while position != 0, so the bar an INTRABAR
    # fill lands on is never seen — and for a target fill that is exactly the bar with the
    # biggest favorable move. Every one of 175 journalled target-fills had mfe BELOW the
    # target distance it demonstrably reached, which is impossible.
    trade = _tracked(Side.LONG, 100.0, bar_hi=102.0, bar_lo=99.0, exit_px=110.0)
    assert trade.mfe == 10.0     # the fill itself, not the last bar the tracker saw


def test_mfe_includes_the_exit_fill_for_a_short():
    trade = _tracked(Side.SHORT, 100.0, bar_hi=101.0, bar_lo=98.0, exit_px=90.0)
    assert trade.mfe == 10.0


def test_mae_includes_the_exit_fill():
    # The same truncation hid stop fills from MAE.
    trade = _tracked(Side.LONG, 100.0, bar_hi=102.0, bar_lo=99.0, exit_px=95.0)
    assert trade.mae == -5.0


def test_a_worse_exit_never_shrinks_a_peak_already_seen():
    # Exiting below the bar's high must not pull mfe down to the exit.
    trade = _tracked(Side.LONG, 100.0, bar_hi=108.0, bar_lo=99.0, exit_px=101.0)
    assert trade.mfe == 8.0
    assert trade.mae == -1.0
