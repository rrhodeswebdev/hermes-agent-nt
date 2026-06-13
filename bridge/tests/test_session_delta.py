"""Session tagging (RTH/ETH) + volume-normalized delta_ratio exposed in the context."""

from datetime import UTC, datetime

from hermes_bridge.indicators import build_context, session_for_ts
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
