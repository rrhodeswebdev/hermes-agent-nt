"""Engine half of the counterfactual loop: record the entry triggers the brain armed but
did NOT fire, replay them forward, and resolve the outcome into the DeclineLog.

Trunk-native: the source is an unfired plan EntryTrigger (the trunk re-arms every bar, so
the recorder dedups by band). The old prefilter-candidate / ARM_PLAN sources were dropped.
"""

from __future__ import annotations

from hermes_bridge.config import BridgeConfig
from hermes_bridge.engine import TradingEngine
from hermes_bridge.journal import DeclineLog, JournalStore
from hermes_bridge.models import Action, Decision
from hermes_bridge.plan import EntryTrigger, TradePlan
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, synthetic_bars


class _StubAgent:
    def decide(self, req):
        return Decision(action=Action.WAIT, rationale="stub")

    def strategy_source(self):
        return "custom"


class _StubPlanner:
    """Returns one fixed armed plan; no real between-bars analysis."""

    def __init__(self, plan: TradePlan) -> None:
        self._plan = plan

    def current_plan(self):
        return self._plan

    def consume(self, plan):
        self._plan = None

    def schedule_plan_analysis(self, preq):
        pass

    def schedule_session_analysis(self, history, preq, *, force=False):
        pass

    def is_analyzing_session(self):
        return False


def _long_plan(basis_ts: float) -> TradePlan:
    # Buy-the-dip band 3990-3995 with a 16t stop / 24t target (tick 0.25):
    # limit 3995, stop 3991, target 4001.
    return TradePlan(
        mode="seek_entry", based_on_bar_ts=basis_ts,
        triggers=[EntryTrigger(direction="long", min_close=3990.0, max_close=3995.0,
                               qty=1, stop_ticks=16, target_ticks=24, confidence=0.8,
                               rationale="pullback")],
    )


def _cf_engine(tmp_path, *, horizon: int = 20):
    cfg = BridgeConfig()  # defaults: ES / 5m / tick 0.25
    cfg.learning.counterfactuals_enabled = True
    cfg.learning.counterfactual_horizon_bars = horizon
    cfg.learning.declines_path = str(tmp_path / "d.jsonl")
    cfg.strategies.reauthor.enabled = False     # keep the reauthor path out of these tests
    cfg.planner.max_plan_age_bars = 9999        # keep the armed plan active (not stale)
    seed = synthetic_bars(60)
    basis = seed[-1].ts
    eng = TradingEngine(
        cfg, BarStore("ES", "5m"),
        SessionState("ES", "5m", 0.25, 12.5, 500, 400),
        _StubAgent(), RiskGate(cfg), planner=_StubPlanner(_long_plan(basis)),
        journal=JournalStore(str(tmp_path / "j.jsonl")),
        declines=DeclineLog(str(tmp_path / "d.jsonl")),
    )
    for b in seed:
        eng.store.append(b)
    return eng, basis


def test_records_unfired_trigger_with_bracket(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # close above band -> no fire -> record
    assert len(eng._cf_pending) == 1
    p = eng._cf_pending[0]
    assert not p.filled
    assert p.side.value == "LONG"
    assert (p.limit_price, p.stop_price, p.target_price) == (3995.0, 3991.0, 4001.0)


def test_resolves_would_win(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))   # low 3994 touches limit -> fill
    assert eng._cf_pending[0].filled
    eng.on_bar(make_bar(t + 900, 3997, 4002, 3997, 3998))   # high 4002 >= target 4001 -> win
    recs = eng.declines.all()
    assert recs[-1]["outcome"] == "would_win"
    assert recs[-1]["kind"] == "missed_trigger"
    assert len(eng.declines.unreported_wins()) == 1


def test_resolves_would_lose(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))   # fill (low 3994)
    eng.on_bar(make_bar(t + 900, 3996, 3997, 3990, 3996))   # low 3990 <= stop 3991 -> lose
    assert eng.declines.all()[-1]["outcome"] == "would_lose"
    assert eng.declines.unreported_wins() == []


def test_fill_bar_spanning_both_brackets_is_ambiguous(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))   # fill
    eng.on_bar(make_bar(t + 900, 3996, 4002, 3990, 3996))   # spans target 4001 AND stop 3991
    assert eng.declines.all()[-1]["outcome"] == "ambiguous"


def test_never_filled_when_limit_untouched_within_horizon(tmp_path):
    eng, t = _cf_engine(tmp_path, horizon=2)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record (bars_left=2)
    eng.on_bar(make_bar(t + 600, 4005, 4006, 3998, 4005))   # low 3998 > limit; 2 -> 1
    assert eng.declines.all() == []
    eng.on_bar(make_bar(t + 900, 4005, 4006, 3998, 4005))   # 1 -> 0 -> never_filled
    assert eng.declines.all()[-1]["outcome"] == "never_filled"


def test_same_band_rearmed_records_once(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record P1
    eng.on_bar(make_bar(t + 600, 4005, 4006, 3998, 4005))   # same band re-armed, no touch -> dedup
    eng.on_bar(make_bar(t + 900, 4005, 4006, 3998, 4005))
    assert len(eng._cf_pending) == 1                        # one pending, not three


def test_gated_off_records_nothing(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.cfg.learning.counterfactuals_enabled = False
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))
    assert eng._cf_pending == []
    assert eng.declines.all() == []
