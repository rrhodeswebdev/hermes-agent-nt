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
