from hermes_bridge.config import BridgeConfig
from hermes_bridge.models import Action, Decision
from hermes_bridge.resilient_brain import ResilientBrain


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeClaude:
    """Stands in for ClaudeAgentClient. `mode` switches behavior per call."""

    def __init__(self):
        self.mode = "ok"
        self.calls = 0

    def decide(self, req):
        self.calls += 1
        if self.mode == "ok":
            self._health = "OK"
            return Decision(action=Action.ENTER_LONG, rationale="claude")
        self._health = self.mode  # "DOWN"/"THROTTLED"/"TRANSIENT"
        return Decision(action=Action.WAIT, rationale="brain_down:X")

    def propose_plan(self, preq):
        self.calls += 1
        if self.mode == "ok":
            return "CLAUDE_PLAN"
        raise RuntimeError("claude CLI exited 1: authentication failed")

    def analyze_session(self, preq, history):
        if self.mode == "ok":
            return "CLAUDE_BRIEF"
        raise RuntimeError("claude CLI exited 1: rate limit")

    def brain_health(self):
        return getattr(self, "_health", "OK")

    # passthroughs the wrapper must delegate:
    def describe(self):
        return "sonnet"

    def generated_strategy(self):
        return "PLAYBOOK"


class FakeMock:
    def decide(self, req):
        return Decision(action=Action.ENTER_SHORT, rationale="mock")

    def propose_plan(self, preq):
        return "MOCK_PLAN"

    def analyze_session(self, preq, history):
        return "MOCK_BRIEF"


def _brain(cfg=None, clock=None):
    cfg = cfg or BridgeConfig.model_validate(
        {"agent": {"client": "claude", "resilience": {"enabled": True}}})
    return ResilientBrain(FakeClaude(), FakeMock(), cfg, time_fn=clock or FakeClock())


def test_healthy_routes_to_claude():
    b = _brain()
    assert b.propose_plan(None) == "CLAUDE_PLAN"
    assert b.decide(None).rationale == "claude"
    assert b.analyze_session(None, []) == "CLAUDE_BRIEF"
    assert b.brain_health() == "OK"


def test_passthroughs_delegate_to_claude():
    b = _brain()
    assert b.describe() == "sonnet"
    assert b.generated_strategy() == "PLAYBOOK"


def _brain_mock_enabled(clock):
    cfg = BridgeConfig.model_validate({"agent": {"client": "claude", "resilience": {
        "enabled": True, "mock_fallback_enabled": True,
        "mock_after_consecutive_failures": 3, "mock_after_seconds_down": 300}}})
    return ResilientBrain(FakeClaude(), FakeMock(), cfg, time_fn=clock)


def test_degraded_stays_wait_below_threshold():
    clock = FakeClock()
    b = _brain_mock_enabled(clock)
    b._claude.mode = "down"
    assert b.propose_plan(None) is None          # failure 1 → DEGRADED, WAIT
    assert b.brain_health() == "DOWN"
    clock.advance(1000)                          # time satisfied but only 1 failure
    assert b.propose_plan(None) is None          # failure 2 → still DEGRADED
    assert b.brain_health() == "DOWN"


def test_escalates_to_mock_after_count_and_time():
    clock = FakeClock()
    b = _brain_mock_enabled(clock)
    b._claude.mode = "down"
    b.propose_plan(None)                          # fail 1 (t0)
    clock.advance(150)
    b.propose_plan(None)                          # fail 2
    clock.advance(151)
    r = b.propose_plan(None)                      # fail 3, >=300s elapsed → MOCK
    assert r == "MOCK_PLAN"
    assert b.brain_health() == "MOCK"


def test_count_met_but_time_not_stays_degraded():
    clock = FakeClock()
    b = _brain_mock_enabled(clock)
    b._claude.mode = "down"
    for _ in range(5):
        clock.advance(10)
        assert b.propose_plan(None) is None      # 5 fails in 50s
    assert b.brain_health() == "DOWN"            # time threshold (300s) not met


def test_mock_fallback_disabled_never_escalates():
    clock = FakeClock()
    cfg = BridgeConfig.model_validate({"agent": {"client": "claude", "resilience": {
        "enabled": True, "mock_fallback_enabled": False}}})
    b = ResilientBrain(FakeClaude(), FakeMock(), cfg, time_fn=clock)
    b._claude.mode = "down"
    for _ in range(10):
        clock.advance(100)
        assert b.propose_plan(None) is None
    assert b.brain_health() == "DOWN"            # stays Tier-1 WAIT forever


def test_recovers_to_healthy_from_mock():
    clock = FakeClock()
    b = _brain_mock_enabled(clock)
    b._claude.mode = "down"
    b.propose_plan(None)
    clock.advance(150)
    b.propose_plan(None)
    clock.advance(151)
    assert b.propose_plan(None) == "MOCK_PLAN"
    b._claude.mode = "ok"                         # API back
    assert b.propose_plan(None) == "CLAUDE_PLAN"  # next cycle's normal call succeeds
    assert b.brain_health() == "OK"


def test_decide_routes_to_mock_when_down():
    clock = FakeClock()
    b = _brain_mock_enabled(clock)
    b._claude.mode = "down"
    b.decide(None)
    clock.advance(150)
    b.decide(None)
    clock.advance(151)
    assert b.decide(None).rationale == "mock"
