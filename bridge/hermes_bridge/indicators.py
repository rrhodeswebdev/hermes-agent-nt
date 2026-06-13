"""Lightweight, dependency-free technical helpers.

Pure functions over plain lists of floats / Bars so they are trivially testable
and add no heavy runtime deps. The deterministic engine and the MockAgentClient
use these to build the order-flow / price-action context that the LLM also sees.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Bar


def ema(values: list[float], period: int) -> float | None:
    """Exponential moving average of the last `period`+ values. None if too short."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    # Seed with the SMA of the first `period` values, then roll forward.
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(bars: list[Bar], period: int) -> float | None:
    """Average True Range over the last `period` bars (Wilder-style simple mean)."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        trs.append(true_range(bars[i - 1].close, bars[i].high, bars[i].low))
    return sum(trs) / period


def _is_swing_high(bars: list[Bar], c: int, lookback: int) -> bool:
    """Is bar `c` a confirmed swing-high pivot (a high with `lookback` lower highs each side)?"""
    h = bars[c].high
    return all(h > bars[c - j].high for j in range(1, lookback + 1)) and all(
        h > bars[c + j].high for j in range(1, lookback + 1)
    )


def _is_swing_low(bars: list[Bar], c: int, lookback: int) -> bool:
    low = bars[c].low
    return all(low < bars[c - j].low for j in range(1, lookback + 1)) and all(
        low < bars[c + j].low for j in range(1, lookback + 1)
    )


def swing_high(bars: list[Bar], lookback: int = 3) -> float | None:
    """Most recent confirmed swing-high pivot price."""
    n = len(bars)
    if n < 2 * lookback + 1:
        return None
    for c in range(n - lookback - 1, lookback - 1, -1):
        if _is_swing_high(bars, c, lookback):
            return bars[c].high
    return None


def swing_low(bars: list[Bar], lookback: int = 3) -> float | None:
    """Most recent confirmed swing-low pivot price."""
    n = len(bars)
    if n < 2 * lookback + 1:
        return None
    for c in range(n - lookback - 1, lookback - 1, -1):
        if _is_swing_low(bars, c, lookback):
            return bars[c].low
    return None


def swing_pivots(bars: list[Bar], lookback: int = 3) -> list[tuple[float, float, str]]:
    """All confirmed swing pivots, oldest first, as (price, ts, kind) with kind in {high, low}."""
    out: list[tuple[float, float, str]] = []
    for c in range(lookback, len(bars) - lookback):
        if _is_swing_high(bars, c, lookback):
            out.append((bars[c].high, bars[c].ts, "high"))
        if _is_swing_low(bars, c, lookback):
            out.append((bars[c].low, bars[c].ts, "low"))
    return out


def bar_delta(bar: Bar) -> float:
    """Order-flow delta proxy for one bar.

    If the feed supplies bid/ask volume, use the real delta (ask - bid). Otherwise
    approximate with a close-location proxy: where the close sits within the bar's
    range, scaled by volume. Positive => buying pressure.
    """
    if bar.ask_volume is not None and bar.bid_volume is not None:
        return bar.ask_volume - bar.bid_volume
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0
    # close-location value in [-1, 1]: +1 close at high, -1 close at low
    clv = ((bar.close - bar.low) - (bar.high - bar.close)) / rng
    return clv * (bar.volume or 0.0)


def cumulative_delta(bars: list[Bar]) -> float:
    return sum(bar_delta(b) for b in bars)


@dataclass
class MarketContext:
    """Deterministic features handed to the agent each bar (LLM or rules)."""

    last_close: float
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    swing_high: float | None
    swing_low: float | None
    recent_delta: float          # cumulative delta over the recent window
    trend: str                   # "up" | "down" | "flat"
    bars_count: int

    def to_dict(self) -> dict:
        return {
            "last_close": round(self.last_close, 4),
            "ema_fast": _r(self.ema_fast),
            "ema_slow": _r(self.ema_slow),
            "atr": _r(self.atr),
            "swing_high": _r(self.swing_high),
            "swing_low": _r(self.swing_low),
            "recent_delta": round(self.recent_delta, 2),
            "trend": self.trend,
            "bars_count": self.bars_count,
        }


def _r(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def build_context(
    bars: list[Bar],
    *,
    ema_fast: int,
    ema_slow: int,
    atr_period: int,
    delta_window: int = 20,
) -> MarketContext:
    closes = [b.close for b in bars]
    ef = ema(closes, ema_fast)
    es = ema(closes, ema_slow)
    a = atr(bars, atr_period)
    if ef is not None and es is not None:
        trend = "up" if ef > es else "down" if ef < es else "flat"
    else:
        trend = "flat"
    return MarketContext(
        last_close=closes[-1] if closes else 0.0,
        ema_fast=ef,
        ema_slow=es,
        atr=a,
        swing_high=swing_high(bars),
        swing_low=swing_low(bars),
        recent_delta=cumulative_delta(bars[-delta_window:]),
        trend=trend,
        bars_count=len(bars),
    )
