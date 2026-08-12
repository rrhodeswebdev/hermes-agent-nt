"""Setup/regime coherence gate + sizing-confidence cap.

Both come from the 2026-08-11 journal audit of 209 live trades:
  * the brain kept firing continuation setups into a regime its OWN roster said they were
    not for, talking past the hard rule via the rationale narrative (6-loss ETH cluster);
  * reported confidence is ANTI-predictive at the top -- conf >= 0.65 ran 21% wins for
    -$1,176 net, while every lower band was positive.
"""

from hermes_bridge.config import BridgeConfig
from hermes_bridge.engine import setup_regime_mismatch
from hermes_bridge.risk import sizing_confidence

ROSTER = [
    {"name": "Break-and-Go above 29876.5", "regime": "trending", "summary": ""},
    {"name": "Fade 29841-29847 Shelf", "regime": "ranging", "summary": ""},
    {"name": "Untagged Setup", "summary": ""},
]


# ---- setup/regime coherence ------------------------------------------------
def test_mismatch_is_reported_when_live_regime_contradicts_the_setup():
    """The exact 2026-08-11 01:16 loss: a continuation setup the roster declares `trending`
    fired while entry_context said transitional. Fields disagree -> veto."""
    assert setup_regime_mismatch(
        "Break-and-Go above 29876.5", ROSTER, "transitional") == "trending"


def test_no_mismatch_when_regimes_agree():
    assert setup_regime_mismatch("Break-and-Go above 29876.5", ROSTER, "trending") is None


def test_setup_name_matching_is_case_and_space_insensitive():
    assert setup_regime_mismatch("  break-and-go ABOVE 29876.5 ", ROSTER, "trending") is None


def test_unknown_or_missing_inputs_never_veto():
    """Fail OPEN: this gate may only act on a positively-known contradiction. An unknown
    setup, an untagged roster entry, or a missing live regime must all pass through --
    otherwise a roster/plumbing gap would silently halt all trading."""
    assert setup_regime_mismatch("Not In Roster", ROSTER, "transitional") is None
    assert setup_regime_mismatch("Untagged Setup", ROSTER, "transitional") is None
    assert setup_regime_mismatch(None, ROSTER, "transitional") is None
    assert setup_regime_mismatch("Break-and-Go above 29876.5", ROSTER, None) is None
    assert setup_regime_mismatch("Break-and-Go above 29876.5", None, "transitional") is None
    assert setup_regime_mismatch("Break-and-Go above 29876.5", [], "") is None


def test_transitional_setup_in_trending_tape_also_mismatches():
    """Symmetric: the gate is a coherence check, not a transitional blocker."""
    assert setup_regime_mismatch("Fade 29841-29847 Shelf", ROSTER, "trending") == "ranging"


# ---- sizing confidence cap -------------------------------------------------
def test_sizing_confidence_is_capped():
    """Conviction must not buy size in the band where conviction is anti-predictive.
    The cap applies to SIZING only -- the decision's own confidence is untouched."""
    cfg = BridgeConfig()
    cfg.risk.sizing_confidence_cap = 0.62
    assert sizing_confidence(cfg, 0.85) == 0.62
    assert sizing_confidence(cfg, 0.62) == 0.62
    assert sizing_confidence(cfg, 0.55) == 0.55   # below the cap: untouched


def test_no_cap_configured_is_neutral():
    cfg = BridgeConfig()
    assert cfg.risk.sizing_confidence_cap is None   # neutral default
    assert sizing_confidence(cfg, 0.95) == 0.95
    assert sizing_confidence(cfg, None) is None


def test_cap_does_not_raise_a_low_confidence():
    cfg = BridgeConfig()
    cfg.risk.sizing_confidence_cap = 0.62
    assert sizing_confidence(cfg, 0.10) == 0.10
