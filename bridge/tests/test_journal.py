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
