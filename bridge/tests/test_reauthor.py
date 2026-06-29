"""Structure-driven re-authoring: the engine governor that re-runs the pre-session study
when the live market drifts off the authored playbook (trend flip / uncovered regime),
plus the volatility-shock fallback, the freshness ceiling, and the failed-author retry."""

from hermes_bridge.agent_client import MockAgentClient
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import MarketContext
from hermes_bridge.plan import Planner
from hermes_bridge.reauthor import (
    ReauthorState,
    is_volatility_shock,
    record_authored,
    step,
)
from hermes_bridge.risk import RiskGate
from hermes_bridge.store import BarStore
from tests.conftest import make_bar, make_session, synthetic_bars


# ---- pure volatility-shock math ---------------------------------------------
def test_volatility_shock_detection():
    assert is_volatility_shock(25, 10, 2.0) is True    # 2.5× spike
    assert is_volatility_shock(4, 10, 2.0) is True     # 0.4× collapse (≤ 1/2)
    assert is_volatility_shock(15, 10, 2.0) is False   # 1.5×, inside the band
    assert is_volatility_shock(0, 10, 2.0) is True     # cur ATR exactly 0 = total collapse
    assert is_volatility_shock(None, 10, 2.0) is False  # missing current ATR ≠ a reading
    assert is_volatility_shock(10, 0, 2.0) is False    # baseline 0 is meaningless (guarded)


# ---- pure reducer (no engine) ------------------------------------------------
def test_step_is_pure_and_threads_state(cfg):
    # The decision can be exercised by feeding states + contexts in, with no engine.
    rc = cfg.strategies.reauthor
    rc.confirm_bars, rc.min_interval_bars, rc.max_interval_bars = 2, 1, 999
    setups = [{"name": "x", "regime": "trending"}]      # regime covered; isolate the flip
    s0 = record_authored(_ctx("trending", "up"))        # anchored up-trend, clocks at 0
    down = _ctx("trending", "down")
    s1, r1 = step(s0, down, cfg=rc, generated_strategy="pb",
                  generated_strategies=setups, baseline_atr=None)
    assert r1 is None and s1.bars_since_author == 1 and s1.struct_change_bars == 1
    assert s0.bars_since_author == 0                     # the input state is never mutated
    s2, r2 = step(s1, down, cfg=rc, generated_strategy="pb",
                  generated_strategies=setups, baseline_atr=None)
    assert r2 == "trend_flip(up->down) x2b"              # confirmed over confirm_bars → fire
    assert s2.bars_since_author == 2


# ---- engine governor ---------------------------------------------------------
class _SpyAgent(MockAgentClient):
    """Agent-mode rules client with a settable authored playbook + setup roster, so a test
    can pose the brain as "authored / failed to author" and "covers regime X" while counting
    how many pre-session studies the governor triggers."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.session_studies = 0
        self._strategy: str | None = "authored playbook"      # None ⇒ author failed
        self._setups: list[dict] = [{"name": "trend", "regime": "trending"}]

    def generated_strategy(self):
        return self._strategy

    def generated_strategies(self):
        return self._setups

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


def _ctx(regime: str, trend: str, *, atr: float = 10.0, close: float = 5000.0) -> MarketContext:
    """A hand-built context with a chosen regime/trend — lets the structural triggers be
    exercised without depending on synthetic-bar pivot formation."""
    return MarketContext(
        last_close=close, atr=atr, swing_high=close + 5, swing_low=close - 5,
        recent_delta=0.0, regime=regime, trend=trend, bars_count=200,
    )


def _ready(cfg, agent, *, authored=("trending", "up")):
    """An engine seeded with enough bars for ATR, with the structural anchor pinned so the
    governor can be driven directly via _maybe_reauthor. baseline_atr_period defaults far
    above the seed so the shock branch stays out of the way unless a test opts in."""
    engine = _engine(cfg, agent)
    engine.store.replace_history(synthetic_bars(60))
    engine.reauthor_state = ReauthorState(
        authored_regime=authored[0], authored_trend=authored[1],
        bars_since_author=0, struct_change_bars=0)
    return engine


def test_reauthor_fires_on_trend_flip(cfg):
    # Authored under an uptrend; the live trend turns down → re-author once confirmed.
    _tune(cfg, confirm_bars=3, min_interval_bars=2, max_interval_bars=999, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._setups = [{"name": "long", "regime": "trending"}]  # regime is covered; isolate the flip
    engine = _ready(cfg, agent, authored=("trending", "up"))
    down = _ctx("trending", "down")
    base = agent.session_studies
    engine._maybe_reauthor(down)
    engine._maybe_reauthor(down)
    assert agent.session_studies == base                  # confirm window not yet met
    engine._maybe_reauthor(down)
    assert agent.session_studies == base + 1              # 3 consecutive flipped closes → re-author


def test_reauthor_fires_when_regime_uncovered(cfg):
    # Authored only a trending setup; the market goes range-bound → the brain is benched.
    _tune(cfg, confirm_bars=3, min_interval_bars=2, max_interval_bars=999, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._setups = [{"name": "trend", "regime": "trending"}]
    engine = _ready(cfg, agent, authored=("trending", "up"))
    ranging = _ctx("ranging", "flat")                     # "flat" is not a flip; no setup covers it
    base = agent.session_studies
    for _ in range(2):
        engine._maybe_reauthor(ranging)
    assert agent.session_studies == base
    engine._maybe_reauthor(ranging)
    assert agent.session_studies == base + 1              # no_setup_for(ranging) confirmed


def test_reauthor_untagged_setup_covers_any_regime(cfg):
    # A setup with no clean regime tag is assumed to cover any regime → never benched.
    _tune(cfg, confirm_bars=2, min_interval_bars=1, max_interval_bars=999, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._setups = [{"name": "anything", "regime": ""}]
    engine = _ready(cfg, agent, authored=("trending", "up"))
    ranging = _ctx("ranging", "flat")
    base = agent.session_studies
    for _ in range(10):
        engine._maybe_reauthor(ranging)
    assert agent.session_studies == base                  # untagged covers it; no re-author


def test_reauthor_ignores_transient_structure_blip(cfg):
    # A brief divergence that resolves before confirm_bars must NOT re-author (anti-thrash).
    _tune(cfg, confirm_bars=4, min_interval_bars=1, max_interval_bars=999, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._setups = [{"name": "long", "regime": "trending"}]
    engine = _ready(cfg, agent, authored=("trending", "up"))
    up, down = _ctx("trending", "up"), _ctx("trending", "down")
    base = agent.session_studies
    engine._maybe_reauthor(down)
    engine._maybe_reauthor(down)
    engine._maybe_reauthor(up)                            # back in line → counter resets
    engine._maybe_reauthor(down)
    engine._maybe_reauthor(down)
    engine._maybe_reauthor(down)                          # only 3 in a row, confirm_bars=4
    assert agent.session_studies == base


def test_reauthor_respects_min_interval_floor(cfg):
    # Structure is stale every bar, but the debounce floor blocks re-author until it passes.
    _tune(cfg, confirm_bars=1, min_interval_bars=5, max_interval_bars=999, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._setups = [{"name": "long", "regime": "trending"}]
    engine = _ready(cfg, agent, authored=("trending", "up"))
    down = _ctx("trending", "down")
    base = agent.session_studies
    for _ in range(4):                                    # bars_since 1..4, below the floor
        engine._maybe_reauthor(down)
    assert agent.session_studies == base
    engine._maybe_reauthor(down)                          # bars_since == 5 == floor
    assert agent.session_studies == base + 1


def test_reauthor_ceiling_forces_refresh_when_calm(cfg):
    # No structural change and no shock → only the freshness ceiling eventually fires.
    _tune(cfg, confirm_bars=99, min_interval_bars=2, max_interval_bars=6, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._setups = [{"name": "trend", "regime": "trending"}]
    engine = _ready(cfg, agent, authored=("trending", "up"))
    match = _ctx("trending", "up")                        # always fits the anchor → never stale
    base = agent.session_studies
    for _ in range(5):
        engine._maybe_reauthor(match)
    assert agent.session_studies == base
    engine._maybe_reauthor(match)                         # bars_since == 6 == ceiling
    assert agent.session_studies == base + 1


def test_reauthor_retries_failed_author(cfg):
    # The study produced no playbook (generated_strategy is None): retry, don't sit in WAIT.
    _tune(cfg, retry_bars=3, max_interval_bars=999, baseline_atr_period=500)
    agent = _SpyAgent(cfg)
    agent._strategy = None
    engine = _ready(cfg, agent)
    ctx = _ctx("trending", "up")
    base = agent.session_studies
    engine._maybe_reauthor(ctx)
    engine._maybe_reauthor(ctx)                           # bars_since 1..2, below retry_bars
    assert agent.session_studies == base
    engine._maybe_reauthor(ctx)                           # bars_since == 3 == retry_bars
    assert agent.session_studies == base + 1


def test_reauthor_disabled_never_fires(cfg):
    _tune(cfg, enabled=False)
    agent = _SpyAgent(cfg)
    engine = _engine(cfg, agent)
    bars = synthetic_bars(200)
    _seed(engine, bars[:120])
    for b in bars[120:170]:
        engine.on_bar(b)
    assert agent.session_studies == 1                     # only the initial author, never refreshed


def test_reauthor_skips_custom_source(cfg):
    agent = _SpyAgent(cfg)
    agent.set_strategy_source("custom")                   # custom mode authors nothing
    engine = _engine(cfg, agent)
    bars = synthetic_bars(200)
    _seed(engine, bars[:120])                             # custom: study runs but governs nothing
    base = agent.session_studies
    for b in bars[120:170]:
        engine.on_bar(b)
    assert agent.session_studies == base                  # no structural re-author in custom mode


def _bar(ts: float, price: float, rng: float):
    """A bar centred on ``price`` with total range ``rng`` (controls ATR)."""
    half = rng / 2
    return make_bar(ts, price, price + half, price - half, price)


def test_reauthor_volatility_shock_fires_early(cfg):
    # Structure is held quiet (confirm_bars high) and the ceiling is far off, so ONLY a
    # volatility shock can re-author here.
    _tune(cfg, confirm_bars=999, min_interval_bars=3, max_interval_bars=800,
          baseline_atr_period=100, shock_ratio=2.0)
    agent = _SpyAgent(cfg)
    engine = _engine(cfg, agent)
    # Calm baseline history (small ranges) → small baseline ATR.
    calm = [_bar(1_700_000_000 + i * 60, 5000 + i * 0.1, 1.0) for i in range(120)]
    _seed(engine, calm)
    assert agent.session_studies == 1
    before = agent.session_studies
    # A volatility spike (10× the range): baseline stays calm, current ATR jumps past 2× →
    # shock re-author once past min_interval_bars.
    for i in range(8):
        engine.on_bar(_bar(1_700_000_000 + (120 + i) * 60, 5012 + i * 0.1, 10.0))
    assert agent.session_studies > before                 # the shock forced an early re-author


def test_reauthor_config_defaults_are_neutral():
    # New triggers must be OFF in the committed template so default behavior is unchanged.
    from hermes_bridge.config import ReauthorConfig
    rc = ReauthorConfig()
    assert rc.reauthor_after_trade is False
    assert rc.post_trade_min_bars == 2
    assert rc.drift_atr_mult == 0.0


def test_record_authored_stamps_close():
    ctx = _ctx("trending", "up", close=5123.5)
    s = record_authored(ctx)
    assert s.authored_close == 5123.5
    assert s.authored_regime == "trending" and s.authored_trend == "up"
    assert s.bars_since_author == 0 and s.struct_change_bars == 0


def test_price_drift_fires_past_floor(cfg):
    rc = cfg.strategies.reauthor
    rc.confirm_bars, rc.min_interval_bars, rc.max_interval_bars = 99, 3, 999
    rc.drift_atr_mult = 2.0
    setups = [{"name": "x", "regime": "trending"}]      # covered → not stale; isolate drift
    s = ReauthorState(authored_regime="trending", authored_trend="up",
                      authored_close=5000.0, bars_since_author=2)
    far = _ctx("trending", "up", atr=10.0, close=5025.0)   # 25 pts = 2.5x ATR
    s2, r = step(s, far, cfg=rc, generated_strategy="pb",
                 generated_strategies=setups, baseline_atr=None)
    assert r == "price_drift(25.0/10.0)" and s2.bars_since_author == 3


def test_price_drift_blocked_under_floor(cfg):
    rc = cfg.strategies.reauthor
    rc.confirm_bars, rc.min_interval_bars, rc.max_interval_bars = 99, 5, 999
    rc.drift_atr_mult = 2.0
    setups = [{"name": "x", "regime": "trending"}]
    s = ReauthorState(authored_regime="trending", authored_trend="up",
                      authored_close=5000.0, bars_since_author=2)
    far = _ctx("trending", "up", atr=10.0, close=5025.0)
    _, r = step(s, far, cfg=rc, generated_strategy="pb",
                generated_strategies=setups, baseline_atr=None)
    assert r is None                                       # bars_since 3 < floor 5


def test_price_drift_skipped_when_disabled_no_anchor_or_no_atr(cfg):
    rc = cfg.strategies.reauthor
    rc.confirm_bars, rc.min_interval_bars, rc.max_interval_bars = 99, 1, 999
    setups = [{"name": "x", "regime": "trending"}]
    far = _ctx("trending", "up", atr=10.0, close=5025.0)
    s = ReauthorState(authored_regime="trending", authored_trend="up",
                      authored_close=5000.0, bars_since_author=5)
    rc.drift_atr_mult = 0.0                                # disabled
    _, r0 = step(s, far, cfg=rc, generated_strategy="pb",
                 generated_strategies=setups, baseline_atr=None)
    assert r0 is None
    rc.drift_atr_mult = 2.0
    s_no_anchor = ReauthorState(authored_regime="trending", authored_trend="up",
                                authored_close=None, bars_since_author=5)
    _, r1 = step(s_no_anchor, far, cfg=rc, generated_strategy="pb",
                 generated_strategies=setups, baseline_atr=None)
    assert r1 is None                                      # no anchor yet
    no_atr = _ctx("trending", "up", atr=0.0, close=5025.0)
    _, r2 = step(s, no_atr, cfg=rc, generated_strategy="pb",
                 generated_strategies=setups, baseline_atr=None)
    assert r2 is None                                      # no ATR to scale by
