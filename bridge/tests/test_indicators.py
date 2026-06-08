from hermes_bridge.indicators import (
    atr,
    bar_delta,
    build_context,
    ema,
    swing_high,
    swing_low,
)
from tests.conftest import make_bar, synthetic_bars


def test_ema_constant_series():
    assert ema([5.0] * 30, 9) == 5.0


def test_ema_requires_enough_data():
    assert ema([1.0, 2.0], 9) is None


def test_ema_tracks_rising_series():
    vals = [float(i) for i in range(50)]
    e = ema(vals, 9)
    assert e is not None and 40 < e < 49  # lags the latest value


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
        ema_fast=cfg.strategy.ema_fast,
        ema_slow=cfg.strategy.ema_slow,
        atr_period=cfg.strategy.atr_period,
    )
    assert ctx.trend == "up"
    assert ctx.ema_fast is not None and ctx.ema_slow is not None
    assert ctx.ema_fast > ctx.ema_slow
