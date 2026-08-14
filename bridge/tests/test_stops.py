"""Stop-placement + trade-management policy (stops.py): the risk rework's pure core."""

from hermes_bridge.config import (
    BridgeConfig,
    ExecutionConfig,
    InstrumentConfig,
    RiskParams,
    StrategyParams,
)
from hermes_bridge.models import Side
from hermes_bridge.stops import (
    atr_band_stop_ticks,
    clamp_stop_ticks,
    managed_stop_price,
    risk_scale_for_atr,
)


def _cfg(**strat) -> BridgeConfig:
    base = dict(atr_period=14, atr_stop_mult=1.5, atr_target_mult=2.0)
    base.update(strat)
    return BridgeConfig(
        instrument=InstrumentConfig(symbol="MNQ", tick_size=0.25, tick_value=0.50),
        strategy=StrategyParams(**base),
    )


# --------------------------------------------------------------------------- #
# Band clamp                                                                    #
# --------------------------------------------------------------------------- #
def test_clamp_disabled_by_default_is_identity():
    cfg = _cfg()  # min/max_stop_ticks default 0 → unbounded
    assert clamp_stop_ticks(3, cfg) == 3
    assert clamp_stop_ticks(500, cfg) == 500


def test_clamp_floor_lifts_thin_stops():
    cfg = _cfg(min_stop_ticks=24)
    assert clamp_stop_ticks(8, cfg) == 24      # a razor-thin stop is widened to the floor
    assert clamp_stop_ticks(40, cfg) == 40     # a roomy stop is left alone


def test_clamp_ceiling_caps_runaway_stops():
    cfg = _cfg(max_stop_ticks=80)
    assert clamp_stop_ticks(120, cfg) == 80
    assert clamp_stop_ticks(50, cfg) == 50


def test_clamp_never_below_one():
    assert clamp_stop_ticks(0, _cfg()) == 1
    assert clamp_stop_ticks(-5, _cfg()) == 1


def test_atr_band_stop_ticks_scales_then_clamps():
    cfg = _cfg(atr_stop_mult=2.0, min_stop_ticks=24, max_stop_ticks=80)
    # ATR 8 pts → 2.0*8 = 16 pts → 64 ticks (tick 0.25), inside the band.
    assert atr_band_stop_ticks(8.0, cfg) == 64
    # Calm ATR 1 pt → 2.0 pts → 8 ticks, lifted to the 24-tick floor.
    assert atr_band_stop_ticks(1.0, cfg) == 24
    # Wild ATR 20 pts → 40 pts → 160 ticks, capped at the 80-tick ceiling.
    assert atr_band_stop_ticks(20.0, cfg) == 80
    # No ATR → no band stop (caller falls back to the injected default).
    assert atr_band_stop_ticks(None, cfg) is None
    assert atr_band_stop_ticks(0.0, cfg) is None


# --------------------------------------------------------------------------- #
# ATR-regime risk scaling                                                      #
# --------------------------------------------------------------------------- #
def test_risk_scale_disabled_when_factor_is_one():
    cfg = _cfg()  # shock_risk_scale defaults to 1.0
    assert risk_scale_for_atr(100.0, 10.0, cfg) == 1.0  # huge spike, but scaling off


def test_risk_scale_halves_in_a_shock():
    cfg = _cfg()
    cfg.risk = RiskParams(shock_risk_scale=0.5)
    cfg.strategies.reauthor.shock_ratio = 2.0
    assert risk_scale_for_atr(25.0, 10.0, cfg) == 0.5   # 2.5x baseline → shock → halved
    assert risk_scale_for_atr(15.0, 10.0, cfg) == 1.0   # 1.5x baseline → calm → full
    assert risk_scale_for_atr(None, 10.0, cfg) == 1.0   # missing ATR → full
    assert risk_scale_for_atr(25.0, 0.0, cfg) == 1.0    # no baseline → full


# --------------------------------------------------------------------------- #
# Managed stop (breakeven + trail)                                             #
# --------------------------------------------------------------------------- #
def _managed(side, mfe, *, swing_low=None, swing_high=None, **strat):
    cfg = _cfg(**strat)
    return managed_stop_price(
        side=side, entry=100.0, initial_stop_ticks=8, mfe=mfe,
        swing_low=swing_low, swing_high=swing_high, cfg=cfg,
    )  # 8 ticks * 0.25 = 2.0 pts = 1R


def test_managed_off_by_default():
    assert _managed(Side.LONG, mfe=10.0) is None  # breakeven_r defaults to 0 → feature off


def test_managed_none_before_one_r():
    # Reached only +1.9 pts (< 1R = 2.0) → still in the pre-managed phase.
    assert _managed(Side.LONG, mfe=1.9, breakeven_r=1.0) is None


def test_managed_long_pulls_to_breakeven_at_one_r():
    # +2.0 pts == 1R, no trail → stop sits exactly at entry (risk off).
    assert _managed(Side.LONG, mfe=2.0, breakeven_r=1.0) == 100.0


def test_managed_long_trails_above_breakeven():
    # In profit with a higher-low at 101 → trail up to lock in more than breakeven.
    lvl = _managed(Side.LONG, mfe=5.0, swing_low=101.0, breakeven_r=1.0, trail_enabled=True)
    assert lvl == 101.0
    # A higher-low BELOW entry never loosens the stop past breakeven.
    lvl2 = _managed(Side.LONG, mfe=5.0, swing_low=99.0, breakeven_r=1.0, trail_enabled=True)
    assert lvl2 == 100.0


def test_managed_short_mirror():
    # Short breakeven at entry once +1R favorable...
    assert _managed(Side.SHORT, mfe=2.0, breakeven_r=1.0) == 100.0
    # ...then trails DOWN behind a lower-high below entry.
    lvl = _managed(Side.SHORT, mfe=5.0, swing_high=99.0, breakeven_r=1.0, trail_enabled=True)
    assert lvl == 99.0
    # A lower-high above entry never loosens it past breakeven.
    lvl2 = _managed(Side.SHORT, mfe=5.0, swing_high=101.0, breakeven_r=1.0, trail_enabled=True)
    assert lvl2 == 100.0


def test_managed_needs_initial_stop():
    cfg = _cfg(breakeven_r=1.0)
    assert managed_stop_price(
        side=Side.LONG, entry=100.0, initial_stop_ticks=None, mfe=99.0,
        swing_low=None, swing_high=None, cfg=cfg,
    ) is None


# --------------------------------------------------------------------------- #
# Commission-aware breakeven + give-back cap                                    #
# --------------------------------------------------------------------------- #
def _managed_c(side, mfe, *, commission=0.0, swing_low=None, swing_high=None, **strat):
    """managed_stop_price with a commission rate in play. point_value = 0.50/0.25 = $2/pt,
    so a $0.65/contract/side rate costs 2 * 0.65 / 2.0 = 0.65 pts round-trip — which is
    2.6 ticks, so the offset rounds UP to a whole 3 ticks (0.75pt). Rounding DOWN would
    leave the stop short of commission after NinjaTrader snaps it to the tick grid."""
    base = dict(atr_period=14, atr_stop_mult=1.5, atr_target_mult=2.0)
    base.update(strat)
    cfg = BridgeConfig(
        instrument=InstrumentConfig(symbol="MNQ", tick_size=0.25, tick_value=0.50),
        strategy=StrategyParams(**base),
        execution=ExecutionConfig(commission_per_contract=commission),
    )
    return managed_stop_price(
        side=side, entry=100.0, initial_stop_ticks=8, mfe=mfe,
        swing_low=swing_low, swing_high=swing_high, cfg=cfg,
    )  # 8 ticks * 0.25 = 2.0 pts = 1R


def test_breakeven_clears_commission_for_a_long():
    # Price-breakeven (entry) is a GUARANTEED NET LOSS once commission is paid. The stop
    # must sit a round-trip's worth of commission ABOVE entry for a long.
    assert _managed_c(Side.LONG, mfe=2.0, commission=0.65, breakeven_r=1.0) == 100.75


def test_breakeven_clears_commission_for_a_short():
    assert _managed_c(Side.SHORT, mfe=2.0, commission=0.65, breakeven_r=1.0) == 99.25


def test_commission_breakeven_offset_lands_on_the_tick_grid():
    # 0.65pt is 2.6 ticks. A stop must be a tradeable price, and rounding DOWN (2 ticks =
    # 0.50pt = $3.00 on 3 lots) would sit UNDER the $3.90 commission it exists to clear.
    lvl = _managed_c(Side.LONG, mfe=2.0, commission=0.65, breakeven_r=1.0)
    assert (lvl - 100.0) / 0.25 == 3.0


def test_breakeven_is_entry_when_commission_is_zero():
    # Legacy behavior preserved: no commission configured => breakeven is exactly entry.
    assert _managed_c(Side.LONG, mfe=2.0, commission=0.0, breakeven_r=1.0) == 100.0


def test_giveback_cap_off_by_default():
    # Default 0.0 => no give-back level, breakeven behavior unchanged.
    assert _managed_c(Side.LONG, mfe=20.0, breakeven_r=1.0) == 100.0


def test_giveback_cap_rests_at_the_retained_fraction_of_peak():
    # 40% cap => keep 60% of a 20pt peak => stop rests 12pt above entry.
    assert _managed_c(Side.LONG, mfe=20.0, breakeven_r=1.0, giveback_cap_pct=0.40) == 112.0


def test_giveback_cap_short_mirror():
    assert _managed_c(Side.SHORT, mfe=20.0, breakeven_r=1.0, giveback_cap_pct=0.40) == 88.0


def test_giveback_cap_never_sits_below_the_commission_breakeven_floor():
    # A tiny peak: 60% of 1.0pt = 0.6pt, which is INSIDE the 0.75pt commission floor.
    # The floor must win, otherwise the stop locks a guaranteed net loss.
    lvl = _managed_c(Side.LONG, mfe=1.0, commission=0.65, breakeven_r=0.4,
                     giveback_cap_pct=0.40)
    assert lvl == 100.75


def test_giveback_cap_wins_over_a_looser_swing_trail():
    # Swing trail says 101, give-back says 112 → the tighter (higher for a long) wins.
    lvl = _managed_c(Side.LONG, mfe=20.0, swing_low=101.0, breakeven_r=1.0,
                     trail_enabled=True, giveback_cap_pct=0.40)
    assert lvl == 112.0
