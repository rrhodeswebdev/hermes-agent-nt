"""Stop-placement and trade-management policy — pure, config-driven, brain-agnostic.

The risk rework lives here so every path sizes and manages stops the SAME way,
regardless of which brain (mock rules / Claude) is driving:

* ``atr_band_stop_ticks`` / ``clamp_stop_ticks`` — the protective stop is volatility
  scaled (``atr_stop_mult`` × ATR) and then CLAMPED into a tick band
  ``[min_stop_ticks, max_stop_ticks]``. The floor is what stops a 1-minute noise wick
  from tagging a razor-thin stop (the same-bar stop-outs we saw in the journal); the
  ceiling caps the stop in a volatility spike (size then clamps down to fit the
  per-trade dollar cap). A bound of 0 means "unbounded".

* ``risk_scale_for_atr`` — the per-trade dollar budget is scaled down in a volatility
  shock (current ATR ≥ ``shock_ratio`` × the baseline), reusing the same shock_ratio the
  re-author governor uses. Smaller size when the market is wild, full size when calm.

* ``managed_stop_price`` — once a position has run ``breakeven_r`` × (initial stop
  distance) in our favor, the working stop is pulled to breakeven (risk off) and then
  TRAILS behind each new swing. It only ever tightens toward price, so a wider initial
  stop can't turn a trade that worked into a big loss. Before +1R the initial bracket
  (and the structural plan exit) protect the trade unchanged — None is returned.

Nothing here executes orders or reads I/O; the RiskGate and the engine call into it.
"""

from __future__ import annotations

from .config import BridgeConfig
from .models import Side


def clamp_stop_ticks(ticks: int, cfg: BridgeConfig, floor_ticks: int | None = None) -> int:
    """Clamp a stop distance (in ticks) into the configured band. A bound of 0 means
    "unbounded". ``floor_ticks`` (the volatility floor; see ``vol_stop_floor_ticks``) raises
    the effective floor above the fixed ``min_stop_ticks`` when supplied. The ceiling still
    wins last, so even the vol floor can't push a stop past ``max_stop_ticks`` in a spike.
    Never returns < 1 — a zero/negative stop is not protective."""
    lo = cfg.strategy.min_stop_ticks
    if floor_ticks is not None and floor_ticks > lo:
        lo = floor_ticks
    hi = cfg.strategy.max_stop_ticks
    t = int(ticks)
    if lo > 0:
        t = max(t, lo)
    if hi > 0:
        t = min(t, hi)
    return max(1, t)


def vol_stop_floor_ticks(atr: float | None, cfg: BridgeConfig) -> int | None:
    """The volatility-scaled MINIMUM protective-stop distance in ticks:
    ``round(min_stop_atr_mult × ATR / tick_size)``. None when disabled
    (``min_stop_atr_mult`` <= 0) or ATR is unavailable.

    This is the floor the RiskGate enforces on EVERY entry — including one whose stop a brain
    (the Claude plan author especially) set far too tight. A fixed tick floor can't adapt to
    volatility, so a 2-tick stop against a 40pt ATR otherwise slips through; this scales the
    minimum with ATR so the trade gets room to breathe regardless of what the brain proposed."""
    m = cfg.strategy.min_stop_atr_mult
    if m <= 0 or not atr or atr <= 0:
        return None
    tick = cfg.instrument.tick_size or 0.25
    return max(1, round(m * atr / tick))


def atr_band_stop_ticks(atr: float | None, cfg: BridgeConfig) -> int | None:
    """The vol-scaled, band-clamped protective stop in ticks:
    ``round(atr_stop_mult × ATR / tick_size)`` clamped to ``[min_stop_ticks,
    max_stop_ticks]``. None when ATR is unavailable (the caller then falls back to the
    default/injected stop)."""
    if not atr or atr <= 0:
        return None
    tick = cfg.instrument.tick_size or 0.25
    raw = atr * cfg.strategy.atr_stop_mult / tick
    return clamp_stop_ticks(round(raw), cfg)


def risk_scale_for_atr(
    cur_atr: float | None, baseline_atr: float | None, cfg: BridgeConfig
) -> float:
    """Per-trade risk-budget multiplier for the current volatility regime.

    Returns ``shock_risk_scale`` (e.g. 0.5 = halve size) when the current ATR is at least
    ``strategies.reauthor.shock_ratio`` × the longer-window baseline ATR — a volatility
    spike — and 1.0 otherwise. 1.0 (disabled) whenever ``shock_risk_scale`` >= 1, the
    inputs are missing, or the ratio trip is not a real multiple."""
    scale = cfg.risk.shock_risk_scale
    if scale >= 1.0:
        return 1.0
    trip = cfg.strategies.reauthor.shock_ratio
    if not cur_atr or not baseline_atr or baseline_atr <= 0 or trip <= 1:
        return 1.0
    return scale if cur_atr / baseline_atr >= trip else 1.0


def size_for_confidence(
    confidence: float | None, budget_max: int, lo: float, hi: float
) -> int:
    """Scale entry size with the decision's confidence.

    Ramps linearly from 1 contract at ``lo`` (``strategy.min_confidence`` — the lowest
    confidence an entry is taken at) up to ``budget_max`` contracts at ``hi``
    (``full_size_confidence``). Below ``lo`` it is the minimum (1); at/above ``hi`` it is
    the full budget. ``budget_max`` is already the most contracts BOTH the position cap and
    the per-trade dollar budget allow, so the result never exceeds either limit. Returns
    ``budget_max`` (0 or 1) when there is no room to scale, and the minimum (1) when
    ``confidence`` is unknown."""
    if budget_max <= 1:
        return max(0, budget_max)
    if confidence is None:
        return 1
    if hi <= lo:  # degenerate band → step function at the threshold
        return budget_max if confidence >= hi else 1
    frac = (confidence - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    return int(1 + round(frac * (budget_max - 1)))


def managed_stop_price(
    *,
    side: Side,
    entry: float,
    initial_stop_ticks: int | None,
    mfe: float,
    swing_low: float | None,
    swing_high: float | None,
    cfg: BridgeConfig,
) -> float | None:
    """The deterministic "working" stop price for an open position, or None to leave it
    to the resting bracket + structural plan exit.

    The managed phase only ENGAGES once favorable excursion (``mfe``, in points) reaches
    ``breakeven_r`` × the initial stop distance — that is the "+1R" gate. At that point
    the stop is pulled to breakeven (entry); with ``trail_enabled`` it then rides behind
    the most recent swing (the higher-low for a long, the lower-high for a short) whenever
    that locks in MORE than breakeven. The level only ever tightens, so it can never sit
    on the wrong side of price at the moment it engages (price is ~+1R away by then).

    Returns None when the feature is off (``breakeven_r`` <= 0), the initial stop distance
    is unknown, or the trade has not yet reached +1R — leaving the pre-+1R behavior (wide
    initial bracket + the brain's structural exit) exactly as before.
    """
    be_r = cfg.strategy.breakeven_r
    if be_r <= 0 or not initial_stop_ticks or initial_stop_ticks <= 0:
        return None
    tick = cfg.instrument.tick_size or 0.25
    one_r = initial_stop_ticks * tick
    if mfe < be_r * one_r:
        return None  # not yet +1R — pre-managed phase, bracket/structural exit protect it
    trail = cfg.strategy.trail_enabled
    if side == Side.LONG:
        level = entry  # breakeven
        if trail and swing_low is not None and swing_low > level:
            level = swing_low  # trail up behind the higher-low (lock in profit)
        return level
    # SHORT — the stop sits above; breakeven then trails DOWN behind the lower-high.
    level = entry
    if trail and swing_high is not None and swing_high < level:
        level = swing_high
    return level
