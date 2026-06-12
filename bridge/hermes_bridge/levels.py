"""Support/resistance level detection for the chart overlay (`GET /levels`).

Scans the recent bar history for **swing pivots** (a high with `lookback` lower highs
on each side, or a low with `lookback` higher lows on each side), then clusters pivots
that sit within a few ticks of each other into S/R **zones**. Each zone carries how many
pivots touched it (`strength`) and the time span it has been respected
(`first_ts`/`end_ts`), so the NinjaScript overlay can draw thicker/longer lines for the
levels that have mattered most.

Pure functions over a list of `Bar`s — no I/O, fully testable.
"""

from __future__ import annotations

from .models import Bar


def _pivots(bars: list[Bar], lookback: int) -> list[tuple[float, float, str]]:
    """Return confirmed swing pivots as (price, ts, kind) with kind in {high, low}."""
    out: list[tuple[float, float, str]] = []
    n = len(bars)
    for c in range(lookback, n - lookback):
        b = bars[c]
        left = range(c - lookback, c)
        right = range(c + 1, c + lookback + 1)
        if all(b.high > bars[j].high for j in left) and all(b.high > bars[j].high for j in right):
            out.append((b.high, b.ts, "high"))
        if all(b.low < bars[j].low for j in left) and all(b.low < bars[j].low for j in right):
            out.append((b.low, b.ts, "low"))
    return out


def detect_levels(
    bars: list[Bar],
    *,
    lookback: int = 3,
    tick_size: float = 0.25,
    merge_ticks: int = 8,
    min_touches: int = 1,
    max_levels: int = 12,
) -> list[dict]:
    """Cluster swing pivots into S/R zones, strongest first.

    Each returned zone is ``{low, high, strength, first_ts, end_ts, kind}`` where
    ``kind`` is ``support`` / ``resistance`` (by the dominant pivot type) or ``pivot``
    when a level has acted as both. ``strength`` is the pivot (touch) count.
    """
    pivots = _pivots(bars, lookback)
    if not pivots:
        return []
    tol = max(tick_size, 0.0) * max(merge_ticks, 0)

    # Greedy 1-D clustering by price: sort, then start a new cluster whenever the next
    # pivot is more than `tol` above the running cluster's anchor price.
    pivots.sort(key=lambda p: p[0])
    clusters: list[list[tuple[float, float, str]]] = []
    for piv in pivots:
        if clusters and piv[0] - clusters[-1][0][0] <= tol:
            clusters[-1].append(piv)
        else:
            clusters.append([piv])

    levels: list[dict] = []
    for cl in clusters:
        if len(cl) < min_touches:
            continue
        prices = [p[0] for p in cl]
        times = [p[1] for p in cl]
        highs = sum(1 for p in cl if p[2] == "high")
        lows = len(cl) - highs
        if highs > lows:
            kind = "resistance"
        elif lows > highs:
            kind = "support"
        else:
            kind = "pivot"
        levels.append({
            "low": round(min(prices), 6),
            "high": round(max(prices), 6),
            "strength": len(cl),
            "first_ts": min(times),
            "end_ts": max(times),
            "kind": kind,
        })

    # Strongest (most-touched) first; break ties by most recently respected.
    levels.sort(key=lambda lv: (lv["strength"], lv["end_ts"]), reverse=True)
    return levels[:max_levels]
