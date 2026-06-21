"""Session tagging (RTH/ETH) + volume-normalized delta_ratio exposed in the context."""

from datetime import UTC, datetime

from hermes_bridge.indicators import build_context, entry_window_state, session_for_ts
from hermes_bridge.models import Bar
from tests.conftest import synthetic_bars


def _utc_epoch(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def test_session_for_ts_rth_vs_eth():
    # June -> EDT (UTC-4): RTH 09:30-16:00 ET == 13:30-20:00 UTC. 2026-06-10 is a Wednesday.
    assert session_for_ts(_utc_epoch(2026, 6, 10, 14, 0)) == "RTH"   # 10:00 ET
    assert session_for_ts(_utc_epoch(2026, 6, 10, 13, 0)) == "ETH"   # 09:00 ET (pre-open)
    assert session_for_ts(_utc_epoch(2026, 6, 10, 21, 0)) == "ETH"   # 17:00 ET (post-close)
    assert session_for_ts(_utc_epoch(2026, 6, 10, 6, 0)) == "ETH"    # 02:00 ET overnight
    # January -> EST (UTC-5): RTH == 14:30-21:00 UTC. 2026-01-14 is a Wednesday.
    assert session_for_ts(_utc_epoch(2026, 1, 14, 15, 0)) == "RTH"   # 10:00 ET
    assert session_for_ts(_utc_epoch(2026, 1, 14, 22, 0)) == "ETH"   # 17:00 ET
    # Weekend is always ETH (2026-06-13 is a Saturday).
    assert session_for_ts(_utc_epoch(2026, 6, 13, 17, 0)) == "ETH"


def test_delta_ratio_is_volume_independent():
    # Identical close-location (all closes at the high => clv ~ +1), very different volume.
    # delta_ratio is the volume-weighted mean clv, so it should be ~+1 either way.
    def _bars(vol):
        return [Bar(ts=float(i), open=100.0, high=101.0, low=100.0, close=101.0, volume=vol)
                for i in range(30)]

    light = build_context(_bars(50), atr_period=14)
    heavy = build_context(_bars(50000), atr_period=14)
    assert abs(light.recent_delta) < abs(heavy.recent_delta)          # raw magnitude scales
    assert abs(light.delta_ratio - heavy.delta_ratio) < 1e-6          # ratio does not
    assert light.delta_ratio > 0.9


def test_context_exposes_session_and_delta_ratio():
    ctx = build_context(synthetic_bars(60), atr_period=14)
    d = ctx.to_dict()
    assert d["session"] in ("RTH", "ETH")
    assert "delta_ratio" in d
    assert -1.0 <= d["delta_ratio"] <= 1.0


def test_entry_window_state():
    """OPEN / WIND_DOWN (final 30m RTH) / HALTED / NEWS, with the right priority. 2026-06-12 is a
    normal Friday (NOT 06-19 — that is now Juneteenth); June -> EDT (UTC-4), so RTH 09:30-16:00
    ET == 13:30-20:00 UTC."""
    ew = entry_window_state
    open_rth = _utc_epoch(2026, 6, 12, 16, 0)        # 12:00 ET — normal RTH
    wind = _utc_epoch(2026, 6, 12, 19, 45)           # 15:45 ET — final 30 min
    eth = _utc_epoch(2026, 6, 12, 7, 0)              # 03:00 ET — overnight
    assert ew(open_rth) == "OPEN"
    assert ew(eth) == "OPEN"                          # ETH counts as OPEN (entries allowed)
    assert ew(wind) == "WIND_DOWN"
    # Boundaries: 15:30 ET inclusive; 16:00 ET is no longer RTH (-> ETH -> OPEN).
    assert ew(_utc_epoch(2026, 6, 12, 19, 30)) == "WIND_DOWN"   # 15:30 ET
    assert ew(_utc_epoch(2026, 6, 12, 19, 29)) == "OPEN"        # 15:29 ET still open
    assert ew(_utc_epoch(2026, 6, 12, 20, 0)) == "OPEN"         # 16:00 ET -> ETH
    # Halt + news override the time phase; halt outranks news.
    assert ew(wind, halted=True) == "HALTED"
    assert ew(wind, news_blocked=True) == "NEWS"
    assert ew(open_rth, news_blocked=True) == "NEWS"
    assert ew(open_rth, halted=True, news_blocked=True) == "HALTED"


def test_entry_window_closed_on_a_full_holiday():
    """A full market holiday is CLOSED all day (2026-06-19 = Juneteenth). HALTED/NEWS still
    outrank it so an operator stop is never masked by the calendar."""
    ew = entry_window_state
    assert ew(_utc_epoch(2026, 6, 19, 16, 0)) == "CLOSED"   # 12:00 ET
    assert ew(_utc_epoch(2026, 6, 19, 7, 0)) == "CLOSED"    # 03:00 ET — closed all day
    assert ew(_utc_epoch(2026, 6, 19, 16, 0), halted=True) == "HALTED"
    assert ew(_utc_epoch(2026, 6, 19, 16, 0), news_blocked=True) == "NEWS"


def test_entry_window_early_close_winds_down_before_13_00():
    """An early-close half day winds down in the 30 min before 13:00 ET and is CLOSED after
    (2026-11-27 = Black Friday; Nov -> EST (UTC-5))."""
    ew = entry_window_state
    assert ew(_utc_epoch(2026, 11, 27, 16, 0)) == "OPEN"        # 11:00 ET — tradeable morning
    assert ew(_utc_epoch(2026, 11, 27, 17, 29)) == "OPEN"       # 12:29 ET — still open
    assert ew(_utc_epoch(2026, 11, 27, 17, 30)) == "WIND_DOWN"  # 12:30 ET — final 30 min
    assert ew(_utc_epoch(2026, 11, 27, 17, 59)) == "WIND_DOWN"  # 12:59 ET
    assert ew(_utc_epoch(2026, 11, 27, 18, 0)) == "CLOSED"      # 13:00 ET — closed
