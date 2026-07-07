"""Shadow breakeven-tuning evaluator: saved / scratched / no-change classification, the
regime gate, and the feature-off / no-stop guards. Pure logic — mirrors managed_stop_price's
arming (MFE >= r*1R) and the close-based managed exit, without touching the live order path."""

from __future__ import annotations

from hermes_bridge.config import BridgeConfig
from hermes_bridge.engine import TradingEngine
from hermes_bridge.journal import ClosedTrade, DeclineLog, JournalStore
from hermes_bridge.models import Action, Decision
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.shadow_be import shadow_breakeven_outcome
from hermes_bridge.store import BarStore
from tests.conftest import make_bar


class _StubAgent:
    def decide(self, req):
        return Decision(action=Action.WAIT, rationale="stub")

    def strategy_source(self):
        return "custom"


def _trade(side, entry, stop, realized, mfe=0.0, regime="transitional"):
    return ClosedTrade(
        entry_ts=1000.0, exit_ts=2000.0, side=side, qty=1,
        entry_price=entry, exit_price=entry,  # exit_price is unused by the evaluator
        realized_pnl=realized, bars_held=3, mae=0.0, mfe=mfe, trend=regime,
        entry_context={"regime": regime}, rationale="t", stop_price=stop, target_price=0.0,
    )


def test_saved_loser(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5  # arm at 0.5R
    # SHORT entry 100 stop 110 -> 1R=10, arm at +5 favorable (price dips to <= 95).
    t = _trade("SHORT", 100.0, 110.0, realized=-16.0, mfe=6.0)
    bars = [
        make_bar(1100, 99, 100, 94, 96),    # low 94 -> fav 6 >= 5 armed; close 96 < 100 no breach
        make_bar(1200, 97, 101, 97, 101),   # close 101 >= entry -> breakeven exit
    ]
    r = shadow_breakeven_outcome(t, bars, cfg)
    assert r is not None
    assert r["kind"] == "shadow_breakeven"
    assert r["outcome"] == "saved_loser"
    assert r["shadow_pnl"] == 0.0
    assert r["delta"] == 16.0
    assert r["armed"] is True
    assert r["one_r_pts"] == 10.0


def test_scratched_winner(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5
    # LONG entry 100 stop 90 -> 1R=10, arm at +5 (high >= 105). Actual WON (+20) but dipped back.
    t = _trade("LONG", 100.0, 90.0, realized=20.0, mfe=21.0)
    bars = [
        make_bar(1100, 101, 106, 99, 104),   # high 106 -> fav 6 armed; close 104 > 100 no breach
        make_bar(1200, 103, 107, 98, 99),    # close 99 <= entry -> breakeven exit (scratch the win)
    ]
    r = shadow_breakeven_outcome(t, bars, cfg)
    assert r["outcome"] == "scratched_winner"
    assert r["shadow_pnl"] == 0.0
    assert r["delta"] == -20.0


def test_no_change_never_armed(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5
    # SHORT arm at +5; MFE never reaches it (went straight against) -> unchanged.
    t = _trade("SHORT", 100.0, 110.0, realized=-30.0, mfe=3.0)
    bars = [
        make_bar(1100, 101, 104, 97, 103),   # low 97 -> fav 3 < 5, never armed
        make_bar(1200, 104, 109, 103, 108),  # runs against -> actual loss stands
    ]
    r = shadow_breakeven_outcome(t, bars, cfg)
    assert r["outcome"] == "no_change"
    assert r["armed"] is False
    assert r["delta"] == 0.0
    assert r["shadow_pnl"] == -30.0


def test_no_change_armed_but_no_close_breach(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5
    # LONG armed then RAN to target without ever closing back through entry -> BE never fires.
    t = _trade("LONG", 100.0, 90.0, realized=22.0, mfe=15.0)
    bars = [
        make_bar(1100, 101, 106, 101, 105),   # armed (high 106); close 105 > 100
        make_bar(1200, 106, 112, 104, 111),   # close 111 > 100 -> winner preserved
    ]
    r = shadow_breakeven_outcome(t, bars, cfg)
    assert r["outcome"] == "no_change"
    assert r["armed"] is True
    assert r["delta"] == 0.0


def test_regime_gate_trending_returns_none(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5
    t = _trade("SHORT", 100.0, 110.0, realized=-16.0, mfe=6.0, regime="trending")
    bars = [make_bar(1100, 99, 100, 94, 96), make_bar(1200, 97, 101, 97, 101)]
    assert shadow_breakeven_outcome(t, bars, cfg) is None


def test_feature_off_returns_none(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.0  # off
    t = _trade("SHORT", 100.0, 110.0, realized=-16.0, mfe=6.0)
    assert shadow_breakeven_outcome(t, [make_bar(1100, 99, 100, 94, 96)], cfg) is None


def test_no_stop_returns_none(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5
    t = _trade("SHORT", 100.0, 0.0, realized=-16.0, mfe=6.0)   # no attributed stop -> no 1R
    assert shadow_breakeven_outcome(t, [make_bar(1100, 99, 100, 94, 96)], cfg) is None


def test_empty_bars_no_change(cfg):
    cfg.strategy.shadow_breakeven_r_transitional = 0.5
    t = _trade("SHORT", 100.0, 110.0, realized=-16.0, mfe=6.0)
    r = shadow_breakeven_outcome(t, [], cfg)
    assert r["outcome"] == "no_change"
    assert r["armed"] is False
    assert r["delta"] == 0.0


def _engine(tmp_path, r):
    cfg = BridgeConfig()
    cfg.strategy.shadow_breakeven_r_transitional = r
    return TradingEngine(
        cfg, BarStore("ES", "5m"),
        SessionState("ES", "5m", 0.25, 12.5, 500, 400),
        _StubAgent(), RiskGate(cfg),
        journal=JournalStore(str(tmp_path / "j.jsonl")),
        declines=DeclineLog(str(tmp_path / "d.jsonl")),
    )


def test_engine_records_shadow_on_close(tmp_path):
    eng = _engine(tmp_path, r=0.5)
    for b in [make_bar(1100, 99, 100, 94, 96), make_bar(1200, 97, 101, 97, 101)]:
        eng.store.append(b)  # a SHORT that goes green then reverses -> saved loser
    eng._record_breakeven_shadow(_trade("SHORT", 100.0, 110.0, realized=-16.0, mfe=6.0))
    recs = eng.declines.all()
    assert len(recs) == 1
    assert recs[0]["kind"] == "shadow_breakeven"
    assert recs[0]["outcome"] == "saved_loser"


def test_engine_shadow_off_no_record(tmp_path):
    eng = _engine(tmp_path, r=0.0)  # feature off
    eng.store.append(make_bar(1100, 99, 100, 94, 96))
    eng._record_breakeven_shadow(_trade("SHORT", 100.0, 110.0, realized=-16.0, mfe=6.0))
    assert eng.declines.all() == []
