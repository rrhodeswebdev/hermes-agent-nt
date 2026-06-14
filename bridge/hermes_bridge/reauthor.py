"""Structure-driven re-author governor (agent mode).

Decides WHEN the agent should re-run its pre-session study to refresh the authored
playbook while a session is live, and WHY. This is a small state machine — two bar
counters plus the structural anchor the playbook was authored under — lifted out of the
per-bar engine so the trigger logic has one home and can be exercised directly, without
driving bars through the whole `TradingEngine`.

The engine owns the ACT (scheduling the study, which calls `record_authored` to reset the
clocks) and the GUARDS (feature enabled, planner present, agent mode, not already
analysing). The governor owns the DECISION. Triggers, in priority order:

  * ceiling          — the freshness ceiling (`max_interval_bars`) reached even in a calm,
                       unchanging market.
  * trend_flip       — the live trend turned opposite to the authored trend (its
                       directional setups are now on the wrong side), confirmed over
                       `confirm_bars` closes and past the `min_interval_bars` floor.
  * no_setup_for     — no authored setup is tagged for the live regime (the brain is
                       benched), same confirmation + floor.
  * volatility_shock — an ATR spike/collapse mis-scales the playbook's ATR brackets even
                       when structure holds (secondary; past the floor).
  * author_retry     — a failed/empty author left no playbook to trade: re-attempt on a
                       short clock instead of sitting in WAIT forever.
"""

from __future__ import annotations

from .config import ReauthorConfig
from .indicators import MarketContext


def is_volatility_shock(
    cur_atr: float | None, baseline_atr: float | None, shock_ratio: float
) -> bool:
    """An extreme volatility shift: current ATR is ``shock_ratio``× the baseline (a spike)
    or ≤ 1/``shock_ratio`` of it (a collapse). Either way the regime read likely changed."""
    if not cur_atr or not baseline_atr or baseline_atr <= 0 or shock_ratio <= 1:
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


class ReauthorGovernor:
    """Bar-clock + structural-anchor state for the re-author decision (agent mode)."""

    def __init__(self, cfg: ReauthorConfig) -> None:
        self.cfg = cfg
        # Bars since the live playbook was authored — drives the debounce floor, the
        # freshness ceiling, and the failed-author retry clock.
        self._bars_since_author = 0
        # Consecutive closes the live structure has diverged from the authored playbook
        # (reset the moment it fits again), so a one-bar wobble doesn't thrash it.
        self._struct_change_bars = 0
        # The regime/trend the LIVE playbook was authored under, so a flip away from it is
        # detectable. None until the first study records one.
        self._authored_regime: str | None = None
        self._authored_trend: str | None = None

    # ---- read side (telemetry for the engine's log line) --------------------
    @property
    def bars_since_author(self) -> int:
        return self._bars_since_author

    @property
    def authored_regime(self) -> str | None:
        return self._authored_regime

    @property
    def authored_trend(self) -> str | None:
        return self._authored_trend

    # ---- write side ---------------------------------------------------------
    def record_authored(self, ctx: MarketContext) -> None:
        """A fresh study just authored from ``ctx``: anchor the staleness check to THIS read
        and reset the clocks, so the next re-author fires when the live market drifts off the
        new playbook (not the previous one)."""
        self._bars_since_author = 0
        self._struct_change_bars = 0
        self._authored_regime = ctx.regime
        self._authored_trend = ctx.trend

    def evaluate(
        self,
        ctx: MarketContext,
        *,
        generated_strategy: str | None,
        generated_strategies: list[dict] | None,
        baseline_atr: float | None,
    ) -> str | None:
        """The reason to re-author NOW, or None. Advances the bar clocks as a side effect, so
        call exactly once per bar in agent mode (after the engine's guards, never while a study
        is already in flight)."""
        rc = self.cfg
        self._bars_since_author += 1

        # A failed/empty author left no playbook to trade: retry on a short clock instead of
        # sitting in WAIT forever (otherwise the brain is stuck waiting for a study that
        # already finished empty).
        if generated_strategy is None:
            if self._bars_since_author >= rc.retry_bars:
                return "author_retry"
            return None

        # How long the live structure has been at odds with the authored playbook.
        stale = self._playbook_stale(ctx, generated_strategies)
        self._struct_change_bars = self._struct_change_bars + 1 if stale else 0

        past_floor = self._bars_since_author >= rc.min_interval_bars
        if self._bars_since_author >= rc.max_interval_bars:
            return f"ceiling({rc.max_interval_bars}b)"
        if past_floor and stale and self._struct_change_bars >= rc.confirm_bars:
            return f"{stale} x{self._struct_change_bars}b"
        if past_floor and is_volatility_shock(ctx.atr, baseline_atr, rc.shock_ratio):
            return "volatility_shock"
        return None

    def _playbook_stale(
        self, ctx: MarketContext, generated_strategies: list[dict] | None
    ) -> str | None:
        """Why the authored playbook no longer fits the live market, or None if it still does.

        - ``trend_flip``: the live trend turned opposite to the trend the playbook was authored
          under — its directional setups are now on the wrong side.
        - ``no_setup_for``: no authored setup is tagged for the live regime, so the brain has
          nothing to arm (benched) and a fresh playbook is needed for this market.

        A trend read of "flat" (off-trend) is not a flip; an untagged setup covers any regime
        (see ``regime_covered``) so a missing tag never forces a re-author on its own."""
        a_trend = self._authored_trend
        if (a_trend in ("up", "down") and ctx.trend in ("up", "down")
                and ctx.trend != a_trend):
            return f"trend_flip({a_trend}->{ctx.trend})"
        if not regime_covered(ctx.regime, generated_strategies):
            return f"no_setup_for({ctx.regime})"
        return None
