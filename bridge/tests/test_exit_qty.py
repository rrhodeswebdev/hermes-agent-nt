"""EXIT/FLATTEN quantity: a plan exit must close the WHOLE position.

`plan.evaluate_plan` builds its EXIT Decision without a qty (Decision.qty defaults to 0),
relying on the RiskGate's `qty = abs(session.position) if command.qty <= 0` fallback to fill
in the full position. `_to_command` used to coerce every 0 up to 1, which defeated that
fallback: on 2026-09-09 a plan_exit on a 3-lot position queued `EXIT qty=1`. It was masked
only because HermesBridgeStrategy.cs ignores cmd.Qty on EXIT and always flattens — so if the
strategy is ever fixed to honour the quantity, that exit would strand 2 contracts while the
bridge believed it was flat.

The 1-lot floor still applies to ENTRIES, where a 0 would be rejected as `zero_qty` on the
non-auto-sizing path (see test_plan.test_zero_qty_trigger_never_fires).
"""

from hermes_bridge.engine import TradingEngine
from hermes_bridge.models import Action, Decision
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore


def _engine(cfg, position: int) -> TradingEngine:
    store = BarStore("ES", "5m")
    session = SessionState("ES", "5m", 0.25, 12.5, 500.0, 400.0)
    session.position = position
    session.avg_price = 4000.0
    return TradingEngine(cfg, store, session, None, RiskGate(cfg))


def test_plan_exit_without_qty_closes_the_whole_position(cfg):
    # A 3-lot short; plan.py builds EXIT with no qty at all.
    engine = _engine(cfg, -3)
    cmd = engine._to_command(Decision(action=Action.EXIT, rationale="plan_exit(...)"))
    assert cmd.qty == 0, "a no-qty EXIT must stay 0 so the RiskGate fills in the position"
    rd = engine.risk.evaluate(cmd, engine.session, last_price=4000.0)
    assert rd.approved and rd.command is not None
    assert rd.command.qty == 3, "the RiskGate must size the exit to the full position"


def test_plan_exit_long_side_too(cfg):
    engine = _engine(cfg, 2)
    cmd = engine._to_command(Decision(action=Action.EXIT, rationale="plan_exit(...)"))
    rd = engine.risk.evaluate(cmd, engine.session, last_price=4000.0)
    assert rd.command is not None and rd.command.qty == 2


def test_flatten_without_qty_closes_the_whole_position(cfg):
    engine = _engine(cfg, -4)
    cmd = engine._to_command(Decision(action=Action.FLATTEN, rationale="halted"))
    rd = engine.risk.evaluate(cmd, engine.session, last_price=4000.0)
    assert rd.command is not None and rd.command.qty == 4


def test_explicit_exit_qty_is_preserved(cfg):
    # The managed_stop path sets qty=abs(pos) itself; an explicit qty must survive untouched.
    engine = _engine(cfg, -3)
    cmd = engine._to_command(Decision(action=Action.EXIT, qty=3, rationale="managed_stop(...)"))
    assert cmd.qty == 3
    rd = engine.risk.evaluate(cmd, engine.session, last_price=4000.0)
    assert rd.command is not None and rd.command.qty == 3


def test_entry_without_qty_still_gets_the_one_lot_floor(cfg):
    # Regression guard: entries must NOT inherit the 0 — the non-auto-sizing path would
    # reject it as zero_qty. See test_plan.test_zero_qty_trigger_never_fires.
    engine = _engine(cfg, 0)
    for action in (Action.ENTER_LONG, Action.ENTER_SHORT):
        cmd = engine._to_command(Decision(action=action, rationale="plan_trigger(...)"))
        assert cmd.qty == 1, f"{action} with no qty must floor to 1"
