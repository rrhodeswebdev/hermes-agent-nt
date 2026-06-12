"""The prefilter decline-dedup: a Claude "no" on a candidate must suppress Claude
calls for near-identical candidates (same direction, same price zone) until the
memo expires, the price moves materially, or the direction flips."""

from hermes_bridge.engine import TradingEngine
from hermes_bridge.models import Action, Decision
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, synthetic_bars


class StubPrefilter:
    """Always proposes a candidate in the configured direction."""

    def __init__(self) -> None:
        self.action = Action.ENTER_LONG

    def decide(self, req):
        return Decision(action=self.action, confidence=0.9, rationale="stub candidate")


class CountingDecliner:
    """Stands in for Claude: counts calls, always declines."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, req):
        self.calls += 1
        return Decision(action=Action.WAIT, confidence=0.9, rationale="declined")


def _engine(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.agent.prefilter = "mock"   # enables the prefilter path; stub swapped in below
    store = BarStore("ES", "5m")
    session = SessionState("ES", "5m", 0.25, 12.5, 500.0, 400.0)
    agent = CountingDecliner()
    engine = TradingEngine(cfg, store, session, agent, RiskGate(cfg))
    engine._prefilter = StubPrefilter()
    for b in synthetic_bars(60):
        store.append(b)
    last = synthetic_bars(60)[-1]
    return engine, agent, last.ts, last.close


def test_duplicate_decline_suppresses_repeat_calls(cfg):
    engine, agent, t0, c0 = _engine(cfg)
    r1 = engine.on_bar(make_bar(t0 + 300, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 1 and r1.decision.action == Action.WAIT
    # Same zone next bar: answered from the memo, no Claude call.
    r2 = engine.on_bar(make_bar(t0 + 600, c0, c0 + 1, c0 - 1, c0 + 0.25))
    assert agent.calls == 1
    assert r2.decision.rationale.startswith("prefilter:duplicate_decline")


def test_material_price_move_reopens_the_gate(cfg):
    engine, agent, t0, c0 = _engine(cfg)
    engine.on_bar(make_bar(t0 + 300, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 1
    # Far beyond dedup_atr x ATR (synthetic ATR is a few points): Claude re-consulted.
    far = c0 + 50
    engine.on_bar(make_bar(t0 + 600, far, far + 1, far - 1, far))
    assert agent.calls == 2


def test_direction_flip_clears_the_memo(cfg):
    engine, agent, t0, c0 = _engine(cfg)
    engine.on_bar(make_bar(t0 + 300, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 1
    engine._prefilter.action = Action.ENTER_SHORT
    engine.on_bar(make_bar(t0 + 600, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 2


def test_memo_expires_after_dedup_bars(cfg):
    engine, agent, t0, c0 = _engine(cfg)
    engine.on_bar(make_bar(t0 + 300, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 1
    # 5m bars, dedup_bars=5 -> expired after 25 minutes.
    t_late = t0 + 300 + 5 * 300
    engine.on_bar(make_bar(t_late, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 2


def test_dedup_disabled_with_zero_bars(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.agent.prefilter_dedup_bars = 0
    engine, agent, t0, c0 = _engine(cfg)
    engine.on_bar(make_bar(t0 + 300, c0, c0 + 1, c0 - 1, c0))
    engine.on_bar(make_bar(t0 + 600, c0, c0 + 1, c0 - 1, c0))
    assert agent.calls == 2
