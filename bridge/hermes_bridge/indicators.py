"""Lightweight, dependency-free technical helpers.

Pure functions over plain lists of floats / Bars so they are trivially testable
and add no heavy runtime deps. The deterministic engine and the MockAgentClient
use these to build the order-flow / price-action context that the LLM also sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .models import Bar


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


def classify_regime(
    pivots: list[tuple[float, float, str]], atr_value: float | None, last_close: float
) -> tuple[str, str]:
    """Read the market regime from swing **structure** (not moving averages).

    Compares the two most recent swing highs and the two most recent swing lows:

    - **trending** up = higher high AND higher low; down = lower high AND lower low,
    - **ranging** = both the highs and the lows are ~flat (price contained in a band),
    - **transitional** = anything mixed/breaking (e.g. higher high but lower low), or too
      few confirmed pivots to read structure yet.

    A move only counts as higher/lower if it clears a noise tolerance (~¼ ATR, or ~5 bps
    of price when ATR is unknown) so a one-tick wiggle doesn't flip the regime. Returns
    ``(regime, direction)`` with direction in {"up","down","flat"} ("flat" unless trending)."""
    highs = [p[0] for p in pivots if p[2] == "high"]
    lows = [p[0] for p in pivots if p[2] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "transitional", "flat"
    tol = atr_value * 0.25 if atr_value else (abs(last_close) * 0.0005 if last_close else 0.0)
    higher_high = highs[-1] > highs[-2] + tol
    lower_high = highs[-1] < highs[-2] - tol
    higher_low = lows[-1] > lows[-2] + tol
    lower_low = lows[-1] < lows[-2] - tol
    flat_highs = not higher_high and not lower_high
    flat_lows = not higher_low and not lower_low
    if higher_high and higher_low:
        return "trending", "up"
    if lower_high and lower_low:
        return "trending", "down"
    if flat_highs and flat_lows:
        return "ranging", "flat"
    return "transitional", "flat"


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


@dataclass(frozen=True)
class MarketContext:
    """Deterministic features handed to the agent each bar (LLM or rules).

    Regime is read from swing **structure** (see ``classify_regime``), not moving
    averages — ``regime`` is trending/ranging/transitional and ``trend`` is the
    structural direction. ``recent_pivots`` carries the actual swing sequence so the
    brain can see the higher-highs/higher-lows (or lack thereof) for itself."""

    last_close: float
    atr: float | None
    swing_high: float | None
    swing_low: float | None
    recent_delta: float          # cumulative delta over the recent window
    regime: str                  # "trending" | "ranging" | "transitional" (from structure)
    trend: str                   # "up" | "down" | "flat" — structural direction (flat off-trend)
    bars_count: int
    session: str = "ETH"         # "RTH" | "ETH" -- RTH carries far heavier volume
    delta_ratio: float = 0.0     # recent_delta / recent volume, ~[-1,1]; session-independent
    # The recent confirmed swing pivots (price, "high"/"low"), oldest first — the
    # structure the regime read is based on, surfaced so the brain can judge it directly.
    recent_pivots: list[tuple[float, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "last_close": round(self.last_close, 4),
            "atr": _r(self.atr),
            "swing_high": _r(self.swing_high),
            "swing_low": _r(self.swing_low),
            "recent_delta": round(self.recent_delta, 2),
            "delta_ratio": round(self.delta_ratio, 3),
            "regime": self.regime,
            "trend": self.trend,
            "recent_pivots": self.recent_pivots,
            "session": self.session,
            "bars_count": self.bars_count,
        }


def _r(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def build_context(
    bars: list[Bar],
    *,
    atr_period: int,
    swing_lookback: int = 3,
    delta_window: int = 20,
) -> MarketContext:
    closes = [b.close for b in bars]
    last_close = closes[-1] if closes else 0.0
    a = atr(bars, atr_period)
    pivots = swing_pivots(bars, swing_lookback)
    regime, trend = classify_regime(pivots, a, last_close)
    window = bars[-delta_window:]
    rd = cumulative_delta(window)
    vol = sum((b.volume or 0.0) for b in window)
    return MarketContext(
        last_close=last_close,
        atr=a,
        swing_high=swing_high(bars, swing_lookback),
        swing_low=swing_low(bars, swing_lookback),
        recent_delta=rd,
        regime=regime,
        trend=trend,
        bars_count=len(bars),
        session=session_for_ts(bars[-1].ts) if bars else "ETH",
        delta_ratio=(rd / vol) if vol > 0 else 0.0,
        recent_pivots=[(round(p, 4), k) for p, _, k in pivots[-6:]],
    )
