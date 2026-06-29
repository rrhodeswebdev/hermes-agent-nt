"""Structure-driven re-author decision (agent mode) — a pure reducer.

Decides WHEN the agent should re-run its pre-session study to refresh the authored playbook
while a session is live, and WHY. The state is a small immutable value (`ReauthorState`: two
bar counters + the structural anchor the playbook was authored under); the transitions are
pure functions. The engine holds the current `ReauthorState` and threads it through `step`
each bar — there is no mutable governor object, so the trigger logic can be exercised by
feeding states + contexts in and reading states + reasons out.

The engine owns the ACT (scheduling the study, then anchoring via `record_authored`) and the
GUARDS (feature enabled, planner present, agent mode, not already analysing). The reducer owns
the DECISION. Triggers, in priority order:

  * ceiling          — the freshness ceiling (`max_interval_bars`) reached even in a calm,
                       unchanging market.
  * trend_flip       — the live trend turned opposite to the authored trend (its directional
                       setups are now on the wrong side), confirmed over `confirm_bars` closes
                       and past the `min_interval_bars` floor.
  * no_setup_for     — no authored setup is tagged for the live regime (the brain is benched),
                       same confirmation + floor.
  * volatility_shock — an ATR spike/collapse mis-scales the playbook's ATR brackets even when
                       structure holds (secondary; past the floor).
  * author_retry     — a failed/empty author left no playbook to trade: re-attempt on a short
                       clock instead of sitting in WAIT forever.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import ReauthorConfig
from .indicators import MarketContext


def is_volatility_shock(
    cur_atr: float | None, baseline_atr: float | None, shock_ratio: float
) -> bool:
    """An extreme volatility shift: current ATR is ``shock_ratio``× the baseline (a spike)
    or ≤ 1/``shock_ratio`` of it (a collapse). Either way the regime read likely changed."""
    # `is None`, not truthiness: a current ATR of exactly 0.0 is a real reading (a total
    # volatility collapse), not missing data — let it through so the collapse is detected
    # (r = 0 ≤ 1/shock_ratio). baseline_atr <= 0 still guards the division below.
    if cur_atr is None or baseline_atr is None or baseline_atr <= 0 or shock_ratio <= 1:
        return False
    r = cur_atr / baseline_atr
    return r >= shock_ratio or r <= 1.0 / shock_ratio


def regime_covered(live_regime: str, generated_strategies: list[dict] | None) -> bool:
    """Does any authored setup apply in ``live_regime``? An untagged setup (no clean regime
    tag) is treated as covering any regime, so a missing tag never benches the brain."""
    setups = generated_strategies or []
    return any(
        (s.get("regime") or "").strip().lower() in ("", live_regime)
        for s in setups
    )


@dataclass(frozen=True)
class ReauthorState:
    """The re-author clocks + structural anchor, as one immutable value.

    ``bars_since_author`` drives the debounce floor / freshness ceiling / failed-author retry.
    ``struct_change_bars`` counts consecutive closes the live structure has diverged from the
    authored playbook (reset the moment it fits again). ``authored_regime`` / ``authored_trend``
    record the read the LIVE playbook was authored under, so a flip away from it is detectable."""

    bars_since_author: int = 0
    struct_change_bars: int = 0
    authored_regime: str | None = None
    authored_trend: str | None = None
    authored_close: float | None = None


def record_authored(ctx: MarketContext) -> ReauthorState:
    """The state after a fresh study authored from ``ctx``: clocks reset, anchored to THIS read
    so the next re-author fires when the live market drifts off the new playbook."""
    return ReauthorState(
        authored_regime=ctx.regime, authored_trend=ctx.trend, authored_close=ctx.last_close)


def step(
    state: ReauthorState,
    ctx: MarketContext,
    *,
    cfg: ReauthorConfig,
    generated_strategy: str | None,
    generated_strategies: list[dict] | None,
    baseline_atr: float | None,
    just_closed: bool = False,
) -> tuple[ReauthorState, str | None]:
    """Advance the clocks by one bar and return ``(next_state, reason)`` — ``reason`` is the
    trigger to re-author now, or None. Pure: call once per bar in agent mode, after the engine's
    guards and never while a study is already in flight."""
    rc = cfg
    bars_since = state.bars_since_author + 1

    # A failed/empty author left no playbook to trade: retry on a short clock instead of sitting
    # in WAIT forever (struct_change_bars is left untouched until there is a playbook to judge).
    if generated_strategy is None:
        if bars_since >= rc.retry_bars:
            # Fire and restart the retry clock so re-attempts happen every retry_bars bars,
            # not on every bar once the threshold is first crossed.
            return replace(state, bars_since_author=0), "author_retry"
        return replace(state, bars_since_author=bars_since), None

    stale = _playbook_stale(state, ctx, generated_strategies)
    struct_change = state.struct_change_bars + 1 if stale else 0
    next_state = replace(state, bars_since_author=bars_since, struct_change_bars=struct_change)

    past_floor = bars_since >= rc.min_interval_bars
    drift = _price_drift(state, ctx, rc)
    if bars_since >= rc.max_interval_bars:
        reason: str | None = f"ceiling({rc.max_interval_bars}b)"
    elif rc.reauthor_after_trade and just_closed and bars_since >= rc.post_trade_min_bars:
        reason = "post_trade"
    elif past_floor and stale and struct_change >= rc.confirm_bars:
        reason = f"{stale} x{struct_change}b"
    elif past_floor and drift is not None:
        reason = drift
    elif past_floor and is_volatility_shock(ctx.atr, baseline_atr, rc.shock_ratio):
        reason = "volatility_shock"
    else:
        reason = None
    return next_state, reason


def _price_drift(
    state: ReauthorState, ctx: MarketContext, cfg: ReauthorConfig
) -> str | None:
    """Live price has walked >= ``drift_atr_mult`` x ATR from the price the playbook was
    authored at — its level-anchored setups are likely out of reach even when the regime still
    fits. None when disabled (mult <= 0), before the first author (no anchor), or with no ATR
    to scale by (0/None)."""
    if cfg.drift_atr_mult <= 0 or state.authored_close is None or not ctx.atr:
        return None
    drift = abs(ctx.last_close - state.authored_close)
    if drift >= cfg.drift_atr_mult * ctx.atr:
        return f"price_drift({drift:.1f}/{ctx.atr:.1f})"
    return None


def _playbook_stale(
    state: ReauthorState, ctx: MarketContext, generated_strategies: list[dict] | None
) -> str | None:
    """Why the authored playbook no longer fits the live market, or None if it still does.

    - ``trend_flip``: the live trend turned opposite to the trend the playbook was authored
      under — its directional setups are now on the wrong side.
    - ``no_setup_for``: no authored setup is tagged for the live regime, so the brain has
      nothing to arm (benched) and a fresh playbook is needed for this market.

    A trend read of "flat" (off-trend) is not a flip; an untagged setup covers any regime (see
    ``regime_covered``) so a missing tag never forces a re-author on its own."""
    a_trend = state.authored_trend
    if (a_trend in ("up", "down") and ctx.trend in ("up", "down")
            and ctx.trend != a_trend):
        return f"trend_flip({a_trend}->{ctx.trend})"
    if not regime_covered(ctx.regime, generated_strategies):
        return f"no_setup_for({ctx.regime})"
    return None
