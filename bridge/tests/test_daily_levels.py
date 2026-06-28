"""Multi-day reference levels + weekday/ET-clock on the market context.

Gives the brain the multi-hour structure a 200-bar window can't see (prior-day H/L/C,
overnight range, opening range) plus learnable time-of-day context, ported onto the
trunk's structural (EMA-free) MarketContext.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from hermes_bridge.indicators import build_context, daily_levels, et_weekday_clock
from hermes_bridge.views import dashboard_levels
from tests.conftest import make_bar


def _ts(y: int, mo: int, d: int, h: int, mi: int) -> float:
    """Epoch seconds for a UTC wall-clock; the ET offset is applied inside indicators."""
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def _multi_day_bars() -> list:
    # June 2024 is EDT (UTC-4): 09:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC.
    return [
        # Prior RTH day (Wed 2024-06-12): high 106, low 99, close 104.
        make_bar(_ts(2024, 6, 12, 13, 30), 100, 105, 99, 102, 10),
        make_bar(_ts(2024, 6, 12, 19, 59), 102, 106, 101, 104, 10),
        # Overnight ETH after the prior close: high 108, low 103.
        make_bar(_ts(2024, 6, 13, 2, 0), 104, 108, 103, 107, 5),
        # Today RTH (Thu 2024-06-13): opening-range bar (09:35 ET) high 110 low 106.
        make_bar(_ts(2024, 6, 13, 13, 35), 107, 110, 106, 109, 10),
        # Initial-balance-only bar (10:15 ET) high 111 low 106 — inside 09:30-10:30 but
        # past the 09:30-10:00 opening range, so IB high (111) exceeds OR high (110).
        make_bar(_ts(2024, 6, 13, 14, 15), 109, 111, 106, 110, 10),
        # Later RTH (11:00 ET) high 112 low 108 — extends today's range, not the OR/IB.
        make_bar(_ts(2024, 6, 13, 15, 0), 109, 112, 108, 111, 10),
    ]


def test_daily_levels_prior_overnight_today_opening_range():
    lv = daily_levels(_multi_day_bars())
    assert lv["prior_day_high"] == 106
    assert lv["prior_day_low"] == 99
    assert lv["prior_day_close"] == 104
    assert lv["overnight_high"] == 108
    assert lv["overnight_low"] == 103
    assert lv["today_high"] == 112
    assert lv["today_low"] == 106
    assert lv["open_range_high"] == 110
    assert lv["open_range_low"] == 106
    # Initial balance = 09:30-10:30 ET, a superset of the OR: the 10:15 bar lifts the high.
    assert lv["initial_balance_high"] == 111
    assert lv["initial_balance_low"] == 106


def test_daily_levels_empty_is_all_none():
    assert set(daily_levels([]).values()) == {None}


def test_et_weekday_clock():
    # 2024-06-14 16:54 UTC = Fri 12:54 ET (EDT, UTC-4).
    assert et_weekday_clock(_ts(2024, 6, 14, 16, 54)) == ("Fri", "12:54")


def test_build_context_surfaces_levels_and_clock(cfg):
    bars = _multi_day_bars()
    ctx = build_context(
        bars,
        atr_period=cfg.strategy.atr_period,
        swing_lookback=cfg.strategy.swing_lookback,
        level_bars=bars,
    )
    assert ctx.weekday in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert ":" in ctx.clock_et
    assert ctx.levels is not None
    assert ctx.levels["prior_day_high"] == 106
    d = ctx.to_dict()
    assert "weekday" in d and "clock_et" in d and "levels" in d
    # Regime stays the trunk's structural read — this PR does not touch it.
    assert d["regime"] == ctx.regime


def test_dashboard_levels_surfaces_structural_levels(cfg):
    bars = _multi_day_bars()
    ctx = build_context(
        bars,
        atr_period=cfg.strategy.atr_period,
        swing_lookback=cfg.strategy.swing_lookback,
        level_bars=bars,
    )
    st = SimpleNamespace(engine=SimpleNamespace(last_context=ctx))
    lv = dashboard_levels(st)
    assert lv["swing_high"] == ctx.swing_high  # swing pivots still present
    assert lv["prior_day_high"] == 106         # multi-session levels now surfaced too
    assert lv["initial_balance_high"] == 111
    # No context yet -> None (unchanged contract).
    assert dashboard_levels(SimpleNamespace(engine=SimpleNamespace(last_context=None))) is None
