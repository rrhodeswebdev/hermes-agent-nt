"""Engine half of the counterfactual loop: record the entry triggers the brain armed but
did NOT fire, replay them forward, and resolve the outcome into the DeclineLog.

Trunk-native: the source is an unfired plan EntryTrigger (the trunk re-arms every bar, so
the recorder dedups by band). The old prefilter-candidate / ARM_PLAN sources were dropped.
"""

from __future__ import annotations

from hermes_bridge.config import BridgeConfig
from hermes_bridge.engine import PendingCounterfactual, TradingEngine
from hermes_bridge.journal import ClosedTrade, DeclineLog, JournalStore
from hermes_bridge.models import Action, Decision, Side
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


def test_record_carries_timestamps(tmp_path):
    # The persisted decline must carry its full timeline so the outcome can be
    # re-verified later: born_ts (the bar it was declined on = the replay anchor),
    # fill_ts (limit touched), resolved_ts (outcome decided).
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record (anchor)
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))   # fill (low 3994 <= limit 3995)
    eng.on_bar(make_bar(t + 900, 3997, 4002, 3997, 3998))   # resolve (high 4002 >= target 4001)
    rec = eng.declines.all()[-1]
    assert rec["outcome"] == "would_win"
    assert rec["born_ts"] == t + 300
    assert rec["fill_ts"] == t + 600
    assert rec["resolved_ts"] == t + 900


def test_never_filled_record_has_null_fill_ts(tmp_path):
    # A setup the limit never reached resolves never_filled with no fill_ts, but still
    # carries its anchor and the bar that closed the horizon.
    eng, t = _cf_engine(tmp_path, horizon=2)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # record (bars_left=2)
    eng.on_bar(make_bar(t + 600, 4005, 4006, 3998, 4005))   # no touch; 2 -> 1
    eng.on_bar(make_bar(t + 900, 4005, 4006, 3998, 4005))   # 1 -> 0 -> never_filled
    rec = eng.declines.all()[-1]
    assert rec["outcome"] == "never_filled"
    assert rec["fill_ts"] is None
    assert rec["born_ts"] == t + 300
    assert rec["resolved_ts"] == t + 900


# ---- exit replays: score a non-target exit forward on its original bracket ----

def _exit_replay_engine(tmp_path):
    eng, t = _cf_engine(tmp_path)
    eng.cfg.learning.exit_replays_enabled = True
    eng.planner._plan = None            # no armed plan → isolate the exit-replay path
    return eng, t


def _early_exit_long(t):
    # A LONG exited at 4000 whose ORIGINAL target was 4010, stop 3990.
    return PendingCounterfactual(
        kind="early_exit", side=Side.LONG, limit_price=4000.0, stop_price=3990.0,
        target_price=4010.0, born_ts=t, bars_left=20, rationale="exited on delta flip",
        regime="trending", filled=True, entry_price=4000.0, fill_ts=t)


def test_early_exit_would_win_is_exit_left_money(tmp_path):
    eng, t = _exit_replay_engine(tmp_path)
    eng._cf_pending.append(_early_exit_long(t))
    eng.on_bar(make_bar(t + 300, 4002, 4011, 4001, 4009))    # high 4011 >= target 4010
    ee = [r for r in eng.declines.all() if r["kind"] == "early_exit"]
    assert len(ee) == 1 and ee[0]["outcome"] == "would_win"  # the exit LEFT MONEY (shakeout)
    assert ee[0]["born_ts"] == t


def test_early_exit_would_lose_is_exit_correct(tmp_path):
    eng, t = _exit_replay_engine(tmp_path)
    eng._cf_pending.append(_early_exit_long(t))
    eng.on_bar(make_bar(t + 300, 3999, 4001, 3989, 3995))    # low 3989 <= stop 3990
    ee = [r for r in eng.declines.all() if r["kind"] == "early_exit"]
    assert len(ee) == 1 and ee[0]["outcome"] == "would_lose"  # the exit dodged the stop


def _closed(t, exit_price):
    return ClosedTrade(
        entry_ts=t, exit_ts=t + 60, side="LONG", qty=1, entry_price=4000.0,
        exit_price=exit_price, realized_pnl=0.0, bars_held=2, mae=-1.0, mfe=1.0,
        trend="up", entry_context={"regime": "trending"}, rationale="x",
        stop_price=3990.0, target_price=4010.0)


def test_record_exit_replay_gating(tmp_path):
    eng, t = _exit_replay_engine(tmp_path)
    eng._record_exit_replay(_closed(t, 4010.0))            # exited AT target → not early
    assert not eng._cf_pending
    eng.cfg.learning.exit_replays_enabled = False
    eng._record_exit_replay(_closed(t, 4001.0))            # gated off
    assert not eng._cf_pending
    eng.cfg.learning.exit_replays_enabled = True
    eng._record_exit_replay(_closed(t, 4001.0))            # below target + enabled → records
    assert len(eng._cf_pending) == 1
    p = eng._cf_pending[0]
    assert p.kind == "early_exit" and p.target_price == 4010.0 and p.filled


# ---- gate attribution (item 2A): record WHICH gate blocked a would-win + the flow/conf ----

def test_decline_record_attributes_blocking_gate(tmp_path):
    """A blocked setup records WHICH gate stopped it + the delta_ratio/confidence at decline,
    so reflection can cluster 'this gate cost a winner' by gate + session instead of guessing.
    Here a low-confidence trigger is blocked by the min_confidence gate (default 0.55), then
    replays to a would_win — the persisted record must carry that attribution."""
    eng, t = _cf_engine(tmp_path)
    # Same band as _long_plan, but authored at confidence 0.2 — below the 0.55 floor.
    eng.planner._plan = TradePlan(
        mode="seek_entry", based_on_bar_ts=t,
        triggers=[EntryTrigger(direction="long", min_close=3990.0, max_close=3995.0,
                               qty=1, stop_ticks=16, target_ticks=24, confidence=0.2,
                               rationale="low-conf pullback")])
    eng.on_bar(make_bar(t + 300, 3993, 3996, 3990, 3993))  # close in band -> ENTER, conf 0.2 < 0.55
    p = eng._cf_pending[0]
    assert p.suppressed_by == "min_confidence"
    assert p.confidence == 0.2
    eng.on_bar(make_bar(t + 600, 3994, 3998, 3993, 3996))  # low 3993 <= limit 3995 -> fill
    eng.on_bar(make_bar(t + 900, 3997, 4002, 3997, 3998))  # high 4002 >= target 4001 -> would_win
    rec = eng.declines.all()[-1]
    assert rec["outcome"] == "would_win"
    assert rec["suppressed_by"] == "min_confidence"
    assert rec["confidence"] == 0.2
    assert "delta_ratio" in rec


def test_decline_record_carries_sustained_delta_inputs(tmp_path):
    """The decline record stamps the sustained-delta gate's EXACT inputs — the trailing
    windowed-delta signs and the session — captured at the decline bar, so a future rescore of
    the sustained branch reads them off the record instead of reconstructing delta from bars.db
    (whose history-backfill bars carry no bid/ask)."""
    eng, t = _cf_engine(tmp_path)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # close above band -> record
    p = eng._cf_pending[0]
    # the snapshot is the engine's live trailing-sign window at the decline bar
    assert p.delta_signs == tuple(eng._delta_signs[-16:])
    assert p.delta_signs and all(s in (-1, 0, 1) for s in p.delta_signs)
    assert isinstance(p.session, str) and p.session
    # ...and it survives unchanged into the persisted record
    eng.on_bar(make_bar(t + 600, 3997, 3998, 3994, 3996))   # fill
    eng.on_bar(make_bar(t + 900, 3997, 4002, 3997, 3998))   # resolve would_win
    rec = eng.declines.all()[-1]
    assert rec["delta_signs"] == list(p.delta_signs)
    assert rec["session"] == p.session


def test_unblocked_speculative_replay_has_no_gate(tmp_path):
    """A trigger whose price band the bar never reached is a speculative replay, not a
    gate-block: its record carries suppressed_by == '' (only the matched, suppressed trigger
    is attributed)."""
    eng, t = _cf_engine(tmp_path, horizon=2)                # _long_plan, confidence 0.8 (passes)
    eng.on_bar(make_bar(t + 300, 4005, 4006, 4004, 4005))   # close above band -> not triggered
    assert eng._cf_pending[0].suppressed_by == ""
    eng.on_bar(make_bar(t + 600, 4005, 4006, 3998, 4005))   # no touch; 2 -> 1
    eng.on_bar(make_bar(t + 900, 4005, 4006, 3998, 4005))   # 1 -> 0 -> never_filled
    assert eng.declines.all()[-1]["suppressed_by"] == ""
