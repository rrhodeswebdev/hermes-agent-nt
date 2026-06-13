from hermes_bridge.indicators import (
    atr,
    bar_delta,
    build_context,
    classify_regime,
    swing_high,
    swing_low,
)
from tests.conftest import make_bar, synthetic_bars


def test_atr_positive():
    bars = synthetic_bars(60)
    a = atr(bars, 14)
    assert a is not None and a > 0


def test_swing_detection():
    # Build an explicit pivot high at index 5, pivot low at index 11.
    seq = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10, 4, 10, 11, 12]
    bars = [make_bar(i, v, v + 0.5, v - 0.5, v) for i, v in enumerate(seq)]
    assert swing_high(bars) == 20 + 0.5
    assert swing_low(bars) == 4 - 0.5


def test_bar_delta_sign():
    bullish = make_bar(0, 100, 103, 99.5, 102.5, 1000)   # close near high
    bearish = make_bar(0, 100, 100.5, 97, 97.5, 1000)    # close near low
    assert bar_delta(bullish) > 0
    assert bar_delta(bearish) < 0


def test_bar_delta_uses_real_orderflow_when_present():
    b = make_bar(0, 100, 101, 99, 100, 1000)
    b.ask_volume = 700
    b.bid_volume = 300
    assert bar_delta(b) == 400


def test_build_context_uptrend(cfg):
    ctx = build_context(
        synthetic_bars(120),
        atr_period=cfg.strategy.atr_period,
        swing_lookback=cfg.strategy.swing_lookback,
    )
    # Regime is read from swing structure, not EMAs.
    assert ctx.regime == "trending" and ctx.trend == "up"
    assert ctx.swing_high is not None and ctx.swing_low is not None
    assert ctx.recent_pivots  # the structure the read was based on is surfaced


def _pivots(highs_lows):
    """Helper: build a (price, ts, kind) pivot list from [(price, 'high'|'low'), ...]."""
    return [(p, float(i), k) for i, (p, k) in enumerate(highs_lows)]


def test_classify_regime_uptrend_from_structure():
    # Higher highs AND higher lows → trending up.
    pivots = _pivots([(100, "high"), (95, "low"), (110, "high"), (104, "low")])
    assert classify_regime(pivots, atr_value=1.0, last_close=108) == ("trending", "up")


def test_classify_regime_downtrend_from_structure():
    pivots = _pivots([(110, "high"), (104, "low"), (100, "high"), (95, "low")])
    assert classify_regime(pivots, atr_value=1.0, last_close=96) == ("trending", "down")


def test_classify_regime_range_when_swings_flat():
    # Highs ~flat AND lows ~flat (within tolerance) → ranging.
    pivots = _pivots([(100, "high"), (90, "low"), (100.1, "high"), (89.9, "low")])
    assert classify_regime(pivots, atr_value=2.0, last_close=95) == ("ranging", "flat")


def test_classify_regime_transitional_when_mixed_or_sparse():
    # Higher high but lower low (expanding) → transitional, not a clean trend.
    mixed = _pivots([(100, "high"), (95, "low"), (110, "high"), (90, "low")])
    assert classify_regime(mixed, atr_value=1.0, last_close=105)[0] == "transitional"
    # Too few confirmed pivots to read structure.
    assert classify_regime(_pivots([(100, "high")]), atr_value=1.0, last_close=100) == (
        "transitional", "flat")
