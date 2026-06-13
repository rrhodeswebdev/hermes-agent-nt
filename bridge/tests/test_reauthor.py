"""Volatility-adaptive re-authoring: the cadence math (pure) and the engine governor
that re-runs the pre-session study more often when volatile, less when calm."""

from hermes_bridge.agent_client import MockAgentClient
from hermes_bridge.engine import (
    TradingEngine,
    is_volatility_shock,
    reauthor_interval_bars,
)
from hermes_bridge.plan import Planner
from hermes_bridge.risk import RiskGate
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, make_session, synthetic_bars


# ---- pure cadence math -------------------------------------------------------
def test_reauthor_interval_scales_inversely_with_volatility():
    # At the baseline norm (ratio 1) → the base interval.
    assert reauthor_interval_bars(10, 10, 60, 15, 240) == 60
    # Twice as volatile → half the interval; half as volatile → double.
    assert reauthor_interval_bars(20, 10, 60, 15, 240) == 30
    assert reauthor_interval_bars(5, 10, 60, 15, 240) == 120
    # Clamps: very volatile floored at min, very calm capped at max.
    assert reauthor_interval_bars(100, 10, 60, 15, 240) == 15
    assert reauthor_interval_bars(1, 10, 60, 15, 240) == 240
    # No volatility read (too little history) → base fallback.
    assert reauthor_interval_bars(None, 10, 60, 15, 240) == 60
    assert reauthor_interval_bars(10, None, 60, 15, 240) == 60


def test_volatility_shock_detection():
    assert is_volatility_shock(25, 10, 2.0) is True    # 2.5× spike
    assert is_volatility_shock(4, 10, 2.0) is True     # 0.4× collapse (≤ 1/2)
    assert is_volatility_shock(15, 10, 2.0) is False   # 1.5×, inside the band
    assert is_volatility_shock(None, 10, 2.0) is False
    assert is_volatility_shock(10, 0, 2.0) is False


# ---- engine governor ---------------------------------------------------------
class _SpyAgent(MockAgentClient):
    """Agent-mode rules client that reports a live authored playbook (so the re-author
    governor engages) and counts how many pre-session studies it runs."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.session_studies = 0

    def generated_strategy(self):
        return "authored playbook"

    def analyze_session(self, preq, history):
        self.session_studies += 1
        return "brief"


def _engine(cfg, agent):
    planner = Planner(cfg, agent, synchronous=True)  # studies run inline → deterministic
    engine = TradingEngine(cfg, BarStore("ES", "5m"), make_session(cfg), agent,
                           RiskGate(cfg), planner=planner)
    return engine


def _seed(engine, history):
    """Mirror the real flow: the server fills the bar store (replace_history) BEFORE
    on_history, so on_bar's context is built over the full history, not a thin seed."""
    engine.store.replace_history(history)
    engine.on_history(history)


def _tune(cfg, **kw):
    for k, v in kw.items():
        setattr(cfg.strategies.reauthor, k, v)


def test_reauthor_fires_on_the_adaptive_interval(cfg):
    _tune(cfg, base_interval_bars=10, min_interval_bars=5, max_interval_bars=40,
          baseline_atr_period=20)
    agent = _SpyAgent(cfg)
    engine = _engine(cfg, agent)
    bars = synthetic_bars(200)
    _seed(engine, bars[:120])                  # initial author (study #1), resets the clock
    assert agent.session_studies == 1
    for b in bars[120:150]:                    # ~30 bars at ~baseline volatility
        engine.on_bar(b)
    assert agent.session_studies >= 2          # re-authored on the volatility-adaptive cadence


def test_reauthor_disabled_never_fires(cfg):
    _tune(cfg, enabled=False, base_interval_bars=5)
    agent = _SpyAgent(cfg)
    engine = _engine(cfg, agent)
    bars = synthetic_bars(200)
    _seed(engine, bars[:120])
    for b in bars[120:170]:
        engine.on_bar(b)
    assert agent.session_studies == 1          # only the initial author, never refreshed


def test_reauthor_skips_custom_source(cfg):
    _tune(cfg, base_interval_bars=5, baseline_atr_period=20)
    agent = _SpyAgent(cfg)
    agent.set_strategy_source("custom")        # custom mode authors nothing
    engine = _engine(cfg, agent)
    bars = synthetic_bars(200)
    _seed(engine, bars[:120])                  # custom: study runs but governs nothing
    base = agent.session_studies
    for b in bars[120:170]:
        engine.on_bar(b)
    assert agent.session_studies == base       # no volatility-driven re-author in custom mode


def _bar(ts: float, price: float, rng: float):
    """A bar centred on ``price`` with total range ``rng`` (controls ATR)."""
    half = rng / 2
    return make_bar(ts, price, price + half, price - half, price)


def test_reauthor_volatility_shock_fires_early(cfg):
    # Long base interval (won't elapse in this test) — only a volatility SHOCK can re-author.
    _tune(cfg, base_interval_bars=500, min_interval_bars=3, max_interval_bars=800,
          baseline_atr_period=100, shock_ratio=2.0)
    agent = _SpyAgent(cfg)
    engine = _engine(cfg, agent)
    # Calm baseline history (small ranges) → small baseline ATR.
    calm = [_bar(1_700_000_000 + i * 60, 5000 + i * 0.1, 1.0) for i in range(120)]
    _seed(engine, calm)
    assert agent.session_studies == 1
    before = agent.session_studies
    # A volatility spike (10× the range): baseline stays calm, current ATR jumps past 2× →
    # shock re-author once past min_interval_bars, long before the 500-bar base interval.
    for i in range(8):
        engine.on_bar(_bar(1_700_000_000 + (120 + i) * 60, 5012 + i * 0.1, 10.0))
    assert agent.session_studies > before      # the shock forced an early re-author
