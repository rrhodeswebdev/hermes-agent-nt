from fastapi.testclient import TestClient

from hermes_bridge.config import BridgeConfig
from hermes_bridge.levels import detect_levels
from hermes_bridge.server import create_app
from tests.conftest import make_range_bar as _bar, synthetic_bars


def test_no_pivots_returns_empty():
    assert detect_levels([], tick_size=0.25) == []
    # Monotonic ramp has no confirmed pivots (no high/low with lower/higher neighbors both sides).
    ramp = [_bar(i, 100 + i, 99 + i) for i in range(20)]
    assert detect_levels(ramp, lookback=3, tick_size=0.25) == []


def test_detects_and_shapes_a_resistance_pivot():
    # A clean peak at index 3 (a high with 3 lower highs each side) → one resistance zone.
    highs = [10, 11, 12, 20, 12, 11, 10]
    bars = [_bar(float(i), float(h), float(h) - 2) for i, h in enumerate(highs)]
    zones = detect_levels(bars, lookback=3, tick_size=0.25, merge_ticks=8, min_touches=1)
    assert len(zones) == 1
    z = zones[0]
    assert set(z.model_dump()) == {"low", "high", "strength", "first_ts", "end_ts", "kind"}
    assert z.kind == "resistance"
    assert z.strength == 1
    assert z.high == 20.0


def test_nearby_pivots_merge_and_count_strength():
    # Two separate peaks at ~100, within merge tolerance → one zone with strength 2.
    highs = [90, 91, 92, 100, 92, 91, 90, 91, 92, 100.5, 92, 91, 90]
    bars = [_bar(float(i), float(h), float(h) - 2) for i, h in enumerate(highs)]
    zones = detect_levels(bars, lookback=3, tick_size=0.25, merge_ticks=8, min_touches=1)
    merged = [z for z in zones if z.low <= 100 <= z.high]
    assert merged and merged[0].strength == 2
    assert merged[0].first_ts < merged[0].end_ts


def test_min_touches_filters_singletons():
    highs = [10, 11, 12, 20, 12, 11, 10]
    bars = [_bar(float(i), float(h), float(h) - 2) for i, h in enumerate(highs)]
    assert detect_levels(bars, lookback=3, tick_size=0.25, min_touches=2) == []


def test_levels_endpoint_returns_200_not_404(cfg):
    c = TestClient(create_app(cfg))
    bars = synthetic_bars(200)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    r = c.get("/levels")
    assert r.status_code == 200
    body = r.json()
    assert "levels" in body and isinstance(body["levels"], list)
    for z in body["levels"]:
        assert set(z) == {"low", "high", "strength", "first_ts", "end_ts", "kind"}


def test_levels_endpoint_disabled_returns_empty():
    cfg = BridgeConfig()
    cfg.levels.enabled = False
    c = TestClient(create_app(cfg))
    bars = synthetic_bars(120)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    r = c.get("/levels")
    assert r.status_code == 200
    assert r.json()["levels"] == []
