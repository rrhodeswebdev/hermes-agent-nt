"""Lightweight, dependency-free technical helpers.

Pure functions over plain lists of floats / Bars so they are trivially testable
and add no heavy runtime deps. The deterministic engine and the MockAgentClient
use these to build the order-flow / price-action context that the LLM also sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


def swing_high(bars: list[Bar], lookback: int = 3) -> float | None:
    """Most recent confirmed swing-high pivot (a high with `lookback` lower highs each side)."""
    n = len(bars)
    if n < 2 * lookback + 1:
        return None
    for c in range(n - lookback - 1, lookback - 1, -1):
        pivot = bars[c].high
        if all(bars[c].high > bars[c - j].high for j in range(1, lookback + 1)) and all(
            bars[c].high > bars[c + j].high for j in range(1, lookback + 1)
        ):
            return pivot
    return None


def swing_low(bars: list[Bar], lookback: int = 3) -> float | None:
    """Most recent confirmed swing-low pivot."""
    n = len(bars)
    if n < 2 * lookback + 1:
        return None
    for c in range(n - lookback - 1, lookback - 1, -1):
        if all(bars[c].low < bars[c - j].low for j in range(1, lookback + 1)) and all(
            bars[c].low < bars[c + j].low for j in range(1, lookback + 1)
        ):
            return bars[c].low
    return None


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


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _eastern_offset(dt_utc: datetime) -> timedelta:
    """US Eastern offset from UTC (EST -5 / EDT -4), DST-correct without tzdata.

    DST runs 2nd Sunday of March 02:00 -> 1st Sunday of November 02:00 (local time).
    """
    year = dt_utc.year

    def nth_sunday(month: int, n: int) -> datetime:
        first = datetime(year, month, 1, tzinfo=UTC)
        day = 1 + (6 - first.weekday()) % 7 + (n - 1) * 7  # weekday(): Mon=0 .. Sun=6
        return datetime(year, month, day, tzinfo=UTC)

    # Boundaries in UTC: spring-forward 02:00 EST = 07:00 UTC; fall-back 02:00 EDT = 06:00 UTC.
    dst_start = nth_sunday(3, 2).replace(hour=7)
    dst_end = nth_sunday(11, 1).replace(hour=6)
    return timedelta(hours=-4) if dst_start <= dt_utc < dst_end else timedelta(hours=-5)


def session_for_ts(ts: float) -> str:
    """'RTH' if the bar is in the CME equity-index regular session (09:30-16:00 ET,
    Mon-Fri), else 'ETH' (overnight / extended -- typically a fraction of RTH volume).

    Converts via epoch + timedelta rather than datetime.fromtimestamp, which raises
    OSError on Windows for small / out-of-range timestamps.
    """
    dt_utc = _EPOCH + timedelta(seconds=ts)
    et = dt_utc + _eastern_offset(dt_utc)
    if et.weekday() >= 5:  # Saturday / Sunday
        return "ETH"
    minutes = et.hour * 60 + et.minute
    return "RTH" if 570 <= minutes < 960 else "ETH"  # 09:30 = 570, 16:00 = 960


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
    session: str = "ETH"         # "RTH" | "ETH" -- RTH carries far heavier volume
    delta_ratio: float = 0.0     # recent_delta / recent volume, ~[-1,1]; session-independent

    def to_dict(self) -> dict:
        return {
            "last_close": round(self.last_close, 4),
            "ema_fast": _r(self.ema_fast),
            "ema_slow": _r(self.ema_slow),
            "atr": _r(self.atr),
            "swing_high": _r(self.swing_high),
            "swing_low": _r(self.swing_low),
            "recent_delta": round(self.recent_delta, 2),
            "delta_ratio": round(self.delta_ratio, 3),
            "trend": self.trend,
            "session": self.session,
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
    window = bars[-delta_window:]
    rd = cumulative_delta(window)
    vol = sum((b.volume or 0.0) for b in window)
    return MarketContext(
        last_close=closes[-1] if closes else 0.0,
        ema_fast=ef,
        ema_slow=es,
        atr=a,
        swing_high=swing_high(bars),
        swing_low=swing_low(bars),
        recent_delta=rd,
        trend=trend,
        bars_count=len(bars),
        session=session_for_ts(bars[-1].ts) if bars else "ETH",
        delta_ratio=(rd / vol) if vol > 0 else 0.0,
    )
