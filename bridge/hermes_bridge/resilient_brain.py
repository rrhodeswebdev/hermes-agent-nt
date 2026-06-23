"""Resilient brain wrapper — heartbeat + tiered failover around the Claude CLI client.

Composes the real ClaudeAgentClient with a deterministic MockAgentClient and a small
HEALTHY/DEGRADED/MOCK state machine. The per-cycle propose_plan call is the heartbeat: a
clean return = alive; an exception = down. While DEGRADED the wrapper stays WAIT (the brain
fail-safe is preserved); only after a SUSTAINED outage across all Claude models does it
escalate to MOCK so trading continues on rules until Claude returns. All control is code-side
— the brain never self-diagnoses, never picks its model, and has no input into the switch.
"""

from __future__ import annotations

import time

from .agent_client import AgentClient
from .brain_health import DOWN, MOCK, OK, classify_brain_error
from .config import BridgeConfig

HEALTHY, DEGRADED = "HEALTHY", "DEGRADED"


class ResilientBrain(AgentClient):
    def __init__(self, claude: AgentClient, mock: AgentClient, config: BridgeConfig,
                 *, time_fn=time.monotonic) -> None:
        super().__init__(config)
        self._claude = claude
        self._mock = mock
        self._r = config.agent.resilience
        self._now = time_fn
        self._state = HEALTHY
        self._consec = 0
        self._first_fail_ts: float | None = None
        self._status = OK  # the classified status while DEGRADED

    # ---- routing (state-machine logic added in Task 4) ----------------------
    def propose_plan(self, preq):
        try:
            plan = self._claude.propose_plan(preq)
            self._on_result(False, OK)
            return plan
        except Exception as exc:  # noqa: BLE001 — a raised brain call = down
            self._on_result(True, classify_brain_error(exc))
        return self._mock.propose_plan(preq) if self._state == MOCK else None

    def decide(self, req):
        dec = self._claude.decide(req)         # ClaudeAgentClient.decide never raises
        status = self._claude.brain_health()
        self._on_result(status != OK, status)
        if self._state == MOCK:
            return self._mock.decide(req)
        return dec  # HEALTHY → claude's decision; DEGRADED → claude's safe WAIT decision

    def analyze_session(self, preq, history):
        try:
            brief = self._claude.analyze_session(preq, history)
            self._on_result(False, OK)
            return brief
        except Exception as exc:  # noqa: BLE001
            self._on_result(True, classify_brain_error(exc))
        return self._mock.analyze_session(preq, history) if self._state == MOCK else ""

    def _on_result(self, failed: bool, status: str) -> None:
        if not failed:
            self._state = HEALTHY
            self._consec = 0
            self._first_fail_ts = None
            self._status = OK
            return
        now = self._now()
        self._consec += 1
        if self._first_fail_ts is None:
            self._first_fail_ts = now
        self._status = status
        sustained = (
            self._consec >= self._r.mock_after_consecutive_failures
            and (now - self._first_fail_ts) >= self._r.mock_after_seconds_down
        )
        self._state = MOCK if (self._r.mock_fallback_enabled and sustained) else DEGRADED

    # ---- status -------------------------------------------------------------
    def brain_health(self) -> str:
        if self._state == HEALTHY:
            return OK
        if self._state == MOCK:
            return MOCK
        return self._status or DOWN

    # ---- passthroughs (authoring / source / firm live on the Claude client) -
    def describe(self) -> str:
        return self._claude.describe()

    def set_strategy_source(self, source: str) -> None:
        self._claude.set_strategy_source(source)
        self._mock.set_strategy_source(source)

    def strategy_source(self) -> str:
        return self._claude.strategy_source()

    def set_prop_firm_context(self, filename: str | None) -> None:
        self._claude.set_prop_firm_context(filename)

    def prop_firm_context(self) -> str | None:
        return self._claude.prop_firm_context()

    def generated_strategy(self):
        return self._claude.generated_strategy()

    def generated_strategies(self):
        return self._claude.generated_strategies()

    def authoring_status(self):
        return self._claude.authoring_status()

    def clear_generated_strategy(self) -> None:
        self._claude.clear_generated_strategy()
