"""Re-verify a trigger's OWN stated conditions on the FILL bar.

The authored setups state their gates numerically ("delta >= +0.067 and >900v",
"trend = down AND regime = trending") but only the global delta floor was enforced, so a
trigger could fire on its price band alone while its own preconditions were unmet. Live
example 2026-09-10: a SHORT arm stating `volume>=650` and `trend must be down` filled on a
587-volume bar with entry_context trend=flat/regime=transitional. It won, which is the
dangerous kind of violation — the brain's own reflection logged it as a "gate-violating
winner ... do NOT use it as evidence to relax the trend/regime gate".

The gates live ON the trigger as data, so the check compares FIELDS and never parses a
rationale. Each is optional and fails OPEN when unset, so a plan that omits them behaves
exactly as before.
"""

from hermes_bridge.models import Action
from hermes_bridge.plan import EntryTrigger, TradePlan, evaluate_plan
from tests.conftest import make_bar


def _bar(close: float, volume: float = 1000.0):
    return make_bar(1_700_000_000, close, close + 1, close - 1, close, volume)


def _plan(**kw) -> TradePlan:
    return TradePlan(mode="seek_entry", triggers=[
        EntryTrigger(direction="short", max_close=29089.0, confidence=0.6, **kw)
    ])


# --- volume floor ---------------------------------------------------------- #
def test_volume_below_the_trigger_floor_vetoes_the_entry():
    # The live 2026-09-10 case: stated floor 650, fill bar printed 587.
    d = evaluate_plan(_plan(min_volume=650.0), _bar(29087.75, volume=587.0), position=0)
    assert d.action is Action.WAIT
    assert "volume" in d.rationale


def test_volume_at_the_floor_fires():
    d = evaluate_plan(_plan(min_volume=650.0), _bar(29087.75, volume=650.0), position=0)
    assert d.action is Action.ENTER_SHORT


def test_no_volume_floor_fails_open():
    d = evaluate_plan(_plan(), _bar(29087.75, volume=1.0), position=0)
    assert d.action is Action.ENTER_SHORT


# --- trend / regime gates -------------------------------------------------- #
def test_trend_mismatch_vetoes_the_entry():
    d = evaluate_plan(_plan(require_trend="down"), _bar(29087.75), position=0, trend="flat")
    assert d.action is Action.WAIT
    assert "trend" in d.rationale


def test_trend_match_fires():
    d = evaluate_plan(_plan(require_trend="down"), _bar(29087.75), position=0, trend="down")
    assert d.action is Action.ENTER_SHORT


def test_regime_mismatch_vetoes_the_entry():
    d = evaluate_plan(
        _plan(require_regime="trending"), _bar(29087.75), position=0, regime="transitional")
    assert d.action is Action.WAIT
    assert "regime" in d.rationale


def test_regime_match_fires():
    d = evaluate_plan(
        _plan(require_regime="trending"), _bar(29087.75), position=0, regime="trending")
    assert d.action is Action.ENTER_SHORT


def test_unknown_live_trend_or_regime_fails_open():
    # The engine may not have a read yet; a missing live value must never veto.
    d = evaluate_plan(
        _plan(require_trend="down", require_regime="trending"), _bar(29087.75), position=0)
    assert d.action is Action.ENTER_SHORT


# --- combined + precedence ------------------------------------------------- #
def test_the_full_live_case_is_vetoed():
    # All three of the 2026-09-10 short's stated gates, against what actually printed.
    d = evaluate_plan(
        _plan(min_volume=650.0, require_trend="down", require_regime="trending"),
        _bar(29087.75, volume=587.0), position=0, trend="flat", regime="transitional")
    assert d.action is Action.WAIT
    assert d.rationale.startswith("trigger_gate_unmet")


def test_a_gated_trigger_that_does_not_match_on_price_is_untouched():
    # Price never reached the band: the ordinary no_trigger path, not a gate veto.
    d = evaluate_plan(
        _plan(min_volume=650.0), _bar(29200.0, volume=100.0), position=0)
    assert d.action is Action.WAIT
    assert "no_trigger" in d.rationale


def test_gates_do_not_apply_to_exits():
    # manage_position never consults entry gates — exits are never blocked.
    plan = TradePlan(mode="manage_position", triggers=[],
                     exit=__import__("hermes_bridge.plan", fromlist=["ExitRule"]).ExitRule(
                         exit_above=29100.0, rationale="x"))
    d = evaluate_plan(plan, _bar(29150.0, volume=1.0), position=-1,
                      trend="flat", regime="transitional")
    assert d.action is Action.EXIT


# --- delta MARGIN (the trigger's own bar, stricter than the global floor) ---- #
def _long(**kw):
    return TradePlan(mode="seek_entry", triggers=[
        EntryTrigger(direction="long", min_close=29227.5, max_close=29238.0,
                     confidence=0.66, **kw)
    ])


def test_delta_below_the_triggers_own_margin_vetoes():
    # The live 2026-09-10 case: the corpus knew >=0.12 was the discriminator for this shape
    # ("floor-clearing 0.05-0.10 in this same shape LOST"), the arm fired at 0.101 and lost.
    d = evaluate_plan(_long(min_delta=0.12), _bar(29236.25), position=0, delta_ratio=0.101)
    assert d.action is Action.WAIT
    assert "delta" in d.rationale


def test_delta_at_the_margin_fires():
    d = evaluate_plan(_long(min_delta=0.12), _bar(29236.25), position=0, delta_ratio=0.12)
    assert d.action is Action.ENTER_LONG


def test_delta_margin_is_direction_aware_for_shorts():
    plan = TradePlan(mode="seek_entry", triggers=[
        EntryTrigger(direction="short", max_close=29089.0, confidence=0.6, min_delta=0.12)
    ])
    # A SHORT needs delta <= -0.12; a strongly POSITIVE delta must not satisfy it.
    assert evaluate_plan(plan, _bar(29087.0), position=0,
                         delta_ratio=+0.30).action is Action.WAIT
    assert evaluate_plan(plan, _bar(29087.0), position=0,
                         delta_ratio=-0.30).action is Action.ENTER_SHORT


def test_no_delta_margin_fails_open():
    d = evaluate_plan(_long(), _bar(29236.25), position=0, delta_ratio=0.0)
    assert d.action is Action.ENTER_LONG


def test_unknown_live_delta_fails_open():
    d = evaluate_plan(_long(min_delta=0.12), _bar(29236.25), position=0)
    assert d.action is Action.ENTER_LONG
