"""Schema constraints on the risk-critical config fields fire at parse time.

These guard against invalid YAML (negatives, inverted stop bands, out-of-range
confidences) silently loading into the bridge and reaching the RiskGate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_bridge.config import BridgeConfig, DailyGoal, RiskParams, StrategyParams


def test_defaults_and_committed_template_still_valid():
    # All-defaults must construct, and the committed template's risk numbers stay legal.
    BridgeConfig()
    BridgeConfig.model_validate(
        {
            "strategy": {
                "min_stop_ticks": 24,
                "max_stop_ticks": 280,
                "min_stop_atr_mult": 1.5,
                "breakeven_r": 1.0,
                "min_confidence": 0.40,
            },
            "risk": {
                "max_contracts": 5,
                "max_risk_per_trade": 250.0,
                "max_trades_per_day": 20,
                "shock_risk_scale": 0.5,
                "full_size_confidence": 0.85,
            },
            "daily_goal": {"profit_target": 500.0, "max_daily_loss": 400.0},
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_stop_ticks", -1),
        ("max_stop_ticks", -1),
        ("min_stop_atr_mult", -0.1),
        ("breakeven_r", -1.0),
        ("atr_period", 0),
        ("swing_lookback", 0),
        ("atr_stop_mult", 0),
        ("atr_target_mult", 0),
        ("pullback_atr", -0.1),
        ("min_confidence", 1.5),
        ("min_confidence", -0.1),
    ],
)
def test_strategy_field_bounds(field, value):
    with pytest.raises(ValidationError):
        StrategyParams(**{field: value})


def test_inverted_stop_band_rejected():
    with pytest.raises(ValidationError):
        StrategyParams(min_stop_ticks=100, max_stop_ticks=50)


def test_unbounded_band_not_treated_as_inverted():
    # A 0 bound means "unbounded", so [100, 0] and [0, 50] are legal, not inverted.
    StrategyParams(min_stop_ticks=100, max_stop_ticks=0)
    StrategyParams(min_stop_ticks=0, max_stop_ticks=50)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_contracts", 0),
        ("max_risk_per_trade", 0),
        ("max_trades_per_day", 0),
        ("default_stop_ticks", 0),
        ("shock_risk_scale", 0),
        ("shock_risk_scale", -0.5),
        ("full_size_confidence", 1.5),
    ],
)
def test_risk_field_bounds(field, value):
    with pytest.raises(ValidationError):
        RiskParams(**{field: value})


@pytest.mark.parametrize("field", ["profit_target", "max_daily_loss"])
def test_daily_goal_must_be_positive(field):
    with pytest.raises(ValidationError):
        DailyGoal(**{field: 0})
