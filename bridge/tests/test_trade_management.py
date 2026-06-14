"""Deterministic winner-management in the engine: breakeven + structure trail.

These cover the WIRING (open-trade MFE → stops.managed_stop_price → a forced EXIT
command), brain-agnostic and independent of the plan cycle. The managed-stop MATH
itself is unit-tested in test_stops.py.
"""

from hermes_bridge.agent_client import MockAgentClient
from hermes_bridge.config import (
    BridgeConfig,
    DailyGoal,
    InstrumentConfig,
    RiskParams,
    StrategyParams,
)
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.models import Action, Decision, Fill, Side
from hermes_bridge.plan import EntryTrigger, ExitRule, Planner, PlanRequest, TradePlan
from hermes_bridge.replay_sim import ReplaySimulator
from hermes_bridge.risk import RiskGate
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, make_session, synthetic_bars


class _HoldAgent(MockAgentClient):
    """Always WAIT — so the ONLY thing that can produce an exit is the engine's
    deterministic trade manager, never the brain's own structural read."""

    def decide(self, req):
        return Decision(action=Action.WAIT, rationale="hold")


def _managed_cfg(**strat) -> BridgeConfig:
    base = dict(atr_period=14, swing_lookback=3, min_confidence=0.55)
    base.update(strat)
    return BridgeConfig(
        strategy_id="test-es",
        instrument=InstrumentConfig(symbol="ES", timeframe="5m", tick_size=0.25, tick_value=12.5),
        strategy=StrategyParams(**base),
        risk=RiskParams(max_contracts=2, max_risk_per_trade=250.0, default_stop_ticks=16),
        daily_goal=DailyGoal(profit_target=500.0, max_daily_loss=400.0),
    )


def _flat_seed(entry: float, n: int = 80, start_ts: int = 1_700_000_000) -> list:
    """A flat ±0.75pt zigzag at `entry` so ATR > 0 and the swings sit just BELOW/above entry
    (not 40pts away like the drifting synthetic series) — keeps trail tests deterministic."""
    bars = []
    for i in range(n):
        mid = entry + (0.75 if i % 2 else -0.75)
        bars.append(make_bar(start_ts + i * 300, mid, mid + 1.0, mid - 1.0, mid))
    return bars


def _open_long(cfg, entry: float, stop_ticks: int = 8):
    """An engine holding an open long at `entry` with a known 1R, no planner. Seeded flat at
    `entry` so structural swings hug the entry price and don't skew the trail."""
    store = BarStore("ES", "5m")
    seed = _flat_seed(entry)
    store.replace_history(seed)
    session = make_session(cfg)
    session.position = 1
    session.avg_price = entry
    engine = TradingEngine(cfg, store, session, _HoldAgent(cfg), RiskGate(cfg))
    engine._active_stop_ticks = stop_ticks
    ctx = build_context(seed, atr_period=cfg.strategy.atr_period,
                        swing_lookback=cfg.strategy.swing_lookback)
    engine.tracker.on_entry(ts=seed[-1].ts, side=Side.LONG, qty=1, price=entry,
                            context=ctx, rationale="entry")
    return engine, seed[-1].ts


def test_managed_exit_pulls_to_breakeven_after_one_r():
    cfg = _managed_cfg(breakeven_r=1.0, trail_enabled=False)  # 1R = 8 * 0.25 = 2.0 pts
    entry = 4000.0
    engine, ts = _open_long(cfg, entry, stop_ticks=8)

    # Bar that runs only +1.0 pt (< 1R) then sits above entry → still pre-managed, hold.
    r1 = engine.on_bar(make_bar(ts + 300, entry, entry + 1.0, entry - 0.5, entry + 0.5))
    assert r1.command is None
    assert r1.decision.action is Action.WAIT

    # Bar that runs +3.0 pts (>= 1R) and closes back AT entry → breakeven stop fires.
    r2 = engine.on_bar(make_bar(ts + 600, entry + 0.5, entry + 3.0, entry - 0.25, entry))
    assert r2.command is not None
    assert r2.command.action is Action.EXIT
    assert r2.command.qty == 1
    assert "managed_stop" in r2.decision.rationale


def test_no_managed_exit_when_feature_disabled():
    cfg = _managed_cfg(breakeven_r=0.0)  # winner-management off (legacy static bracket)
    entry = 4000.0
    engine, ts = _open_long(cfg, entry, stop_ticks=8)
    # Same +1R-then-return-to-entry bar: with the feature off the engine does NOT exit
    # (the resting bracket alone protects the trade).
    r = engine.on_bar(make_bar(ts + 300, entry + 0.5, entry + 3.0, entry - 0.25, entry))
    assert r.command is None
    assert r.decision.action is Action.WAIT


def test_managed_exit_needs_one_r_progress():
    cfg = _managed_cfg(breakeven_r=1.0)
    entry = 4000.0
    engine, ts = _open_long(cfg, entry, stop_ticks=8)
    # Price dips to entry on the FIRST bar without ever reaching +1R favorable: the managed
    # stop has not engaged yet, so the engine holds (the wide bracket is still the stop).
    r = engine.on_bar(make_bar(ts + 300, entry, entry + 0.5, entry - 0.5, entry))
    assert r.command is None


def test_trail_ratchets_and_never_loosens():
    """The trailed stop only tightens: a prior high-water level holds even when the current
    bar's structure would compute a looser one."""
    cfg = _managed_cfg(breakeven_r=1.0, trail_enabled=True)
    entry = 4000.0
    engine, ts = _open_long(cfg, entry, stop_ticks=8)  # 1R = 2.0 pts
    engine._managed_level = 4001.0  # simulate a stop already trailed up to 4001 on a prior bar

    # This bar runs past +1R (high 4002.5 → mfe 2.5) and closes at 4000.5: ABOVE breakeven
    # (4000) but BELOW the ratcheted 4001 → the ratchet must still trigger the exit.
    r = engine.on_bar(make_bar(ts + 300, 4001.0, 4002.5, 4000.4, 4000.5))
    assert r.command is not None and r.command.action is Action.EXIT
    assert engine._managed_level == 4001.0  # held the tighter level, did not loosen to 4000


# --------------------------------------------------------------------------- #
# 1R memory: promoted at fill, cleared on flat / never left stale              #
# --------------------------------------------------------------------------- #
class _EnterOnceLong(MockAgentClient):
    """Arms an always-firing long entry with a fixed 8-tick stop; holds in manage mode."""

    def propose_plan(self, preq: PlanRequest) -> TradePlan:
        if preq.mode == "manage_position":
            return TradePlan(mode="manage_position",
                             exit=ExitRule(exit_below=0.0, rationale="never"))
        return TradePlan(mode="seek_entry", bias="long", triggers=[EntryTrigger(
            direction="long", min_close=0.0, qty=1, stop_ticks=8, target_ticks=16,
            confidence=0.9, rationale="always")])


def test_active_stop_promoted_on_fill_and_cleared_on_flat(cfg):
    session = make_session(cfg)
    planner = Planner(cfg, _EnterOnceLong(cfg), synchronous=True)
    engine = TradingEngine(cfg, BarStore("ES", "5m"), session, _EnterOnceLong(cfg),
                           RiskGate(cfg), planner=planner)
    bars = synthetic_bars(4)
    engine.on_bar(bars[0])                       # arms the entry plan
    r1 = engine.on_bar(bars[1])                  # entry fires
    assert r1.command is not None and r1.command.action is Action.ENTER_LONG
    assert engine._active_stop_ticks is None     # NOT set at approval — only at fill

    engine.on_fill(Fill(side=Side.LONG, qty=1, price=bars[1].close, ts=bars[1].ts))
    assert engine._active_stop_ticks == 8        # promoted from the matching pending entry

    engine.on_fill(Fill(side=Side.SHORT, qty=1, price=bars[1].close + 5, ts=bars[2].ts))
    assert engine._active_stop_ticks is None     # cleared on flat
    assert engine._managed_level is None


def test_unattributed_fill_leaves_no_stale_1r(cfg):
    """A fill with no matching pending entry (manual / dropped order that filled anyway) must
    not arm breakeven off a guessed 1R — leave it None so the resting bracket protects it."""
    session = make_session(cfg)
    engine = TradingEngine(cfg, BarStore("ES", "5m"), session, MockAgentClient(cfg),
                           RiskGate(cfg))
    engine.on_bar(synthetic_bars(60)[-1])        # establishes last_context, no pending entry
    engine.on_fill(Fill(side=Side.LONG, qty=1, price=4000.0, ts=synthetic_bars(60)[-1].ts))
    assert engine._active_stop_ticks is None


def test_replay_runs_with_rework_enabled(cfg):
    """End-to-end: the stop band + winner-management config drives the same replay path the
    live bridge uses, finds entries, and respects the trade cap. (The shock-scaling REJECT
    path is unit-tested in test_risk.py; here it's left neutral so the ES-priced synthetic
    fixture — where a wide stop is dear — still admits entries to exercise the managed path.)"""
    cfg.strategy.min_stop_ticks = 6      # band floor active
    cfg.strategy.max_stop_ticks = 40     # band ceiling active
    cfg.strategy.breakeven_r = 1.0       # breakeven + trail active
    cfg.strategy.trail_enabled = True
    sim = ReplaySimulator(cfg)
    report = sim.run(synthetic_bars(400), warmup=50)
    assert report.entries > 0
    assert report.trades_today <= cfg.risk.max_trades_per_day
    assert isinstance(report.realized_pnl, float)
