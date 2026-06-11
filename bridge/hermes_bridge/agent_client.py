"""Agent clients — the decision brain behind the engine.

`AgentClient` is the interface the engine calls each bar. Two real implementations:

* `MockAgentClient` — a deterministic order-flow + price-action rule set. It makes
  the WHOLE system runnable and testable with no LLM and no API key, and serves as
  the safe fallback if Hermes is unavailable. It is also the "rules gate" half of
  the hybrid engine.

* `HermesAgentClient` — delegates judgment to the installed Hermes Agent runtime via
  its documented `AIAgent` programmatic interface (or the `hermes` CLI). The trading
  knowledge/strategy/risk/goal live in Hermes context files; this client just frames
  the request and parses a JSON Decision back. Any failure degrades to WAIT (never
  auto-trades on a malformed/absent response — open positions remain protected by the
  resting bracket stop in NinjaTrader).
"""

from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .config import BridgeConfig
from .indicators import MarketContext
from .models import AccountState, Action, Bar, Decision


@dataclass
class AgentRequest:
    mode: str  # "seek_entry" | "manage_position"
    context: MarketContext
    recent_bars: list[Bar]
    account: AccountState


class AgentClient(ABC):
    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config

    @abstractmethod
    def decide(self, req: AgentRequest) -> Decision: ...


# --------------------------------------------------------------------------- #
# Deterministic rules client (no LLM)                                         #
# --------------------------------------------------------------------------- #
class MockAgentClient(AgentClient):
    """Trend-pullback with order-flow confirmation.

    seek_entry: in an EMA up/down trend, take a pullback toward the fast EMA that
    is confirmed by cumulative delta and a same-direction bar close. Stops/targets
    are ATR-based. manage_position: exit early if the trend flips against us;
    otherwise let the resting bracket work.
    """

    def decide(self, req: AgentRequest) -> Decision:
        if req.mode == "manage_position":
            return self._manage(req)
        return self._seek_entry(req)

    def _seek_entry(self, req: AgentRequest) -> Decision:
        c = req.context
        p = self.cfg.strategy
        if c.ema_fast is None or c.ema_slow is None or c.atr is None or not req.recent_bars:
            return Decision(action=Action.WAIT, rationale="insufficient_history")

        last = req.recent_bars[-1]
        atr = c.atr
        ef = c.ema_fast
        tol = p.pullback_atr * atr  # how far past the EMA still counts as a "tag"
        stop_ticks = self._ticks(atr * p.atr_stop_mult)
        target_ticks = self._ticks(atr * p.atr_target_mult)

        # Long: uptrend, the bar pulled back to TAG the fast EMA (low reached it),
        # then closed back above it on a bullish bar with non-negative order-flow.
        long_tag = last.low <= ef + tol
        if (c.trend == "up" and long_tag and last.close > ef
                and last.close > last.open and c.recent_delta >= 0):
            return Decision(
                action=Action.ENTER_LONG, confidence=self._confidence(c, +1), qty=1,
                stop_ticks=stop_ticks, target_ticks=target_ticks,
                rationale="uptrend pullback tagged fast EMA, closed back above, +delta",
            )
        # Short: mirror image.
        short_tag = last.high >= ef - tol
        if (c.trend == "down" and short_tag and last.close < ef
                and last.close < last.open and c.recent_delta <= 0):
            return Decision(
                action=Action.ENTER_SHORT, confidence=self._confidence(c, -1), qty=1,
                stop_ticks=stop_ticks, target_ticks=target_ticks,
                rationale="downtrend pullback tagged fast EMA, closed back below, -delta",
            )
        return Decision(action=Action.WAIT, rationale="no setup")

    def _manage(self, req: AgentRequest) -> Decision:
        c = req.context
        pos = req.account.position
        below_slow = c.ema_slow is not None and c.last_close < c.ema_slow
        above_slow = c.ema_slow is not None and c.last_close > c.ema_slow
        # Exit early when momentum/trend flips against the open position.
        if pos > 0 and (c.trend == "down" or (c.recent_delta < 0 and below_slow)):
            return Decision(action=Action.EXIT, confidence=0.6,
                            rationale="long invalidated: trend/delta flipped down")
        if pos < 0 and (c.trend == "up" or (c.recent_delta > 0 and above_slow)):
            return Decision(action=Action.EXIT, confidence=0.6,
                            rationale="short invalidated: trend/delta flipped up")
        return Decision(action=Action.WAIT, rationale="hold; bracket protects position")

    def _confidence(self, c: MarketContext, direction: int) -> float:
        score = 0.5
        if c.ema_fast is not None and c.ema_slow is not None:
            spread = abs(c.ema_fast - c.ema_slow)
            score += min(0.25, spread / (c.atr or 1.0) * 0.1)
        score += min(0.2, abs(c.recent_delta) / 10000.0)
        return round(min(0.95, max(0.5, score)), 3)

    def _ticks(self, price_distance: float) -> int:
        ts = self.cfg.instrument.tick_size or 0.25
        return max(1, round(price_distance / ts))


# --------------------------------------------------------------------------- #
# Hermes Agent client (LLM judgment)                                          #
# --------------------------------------------------------------------------- #
DECISION_INSTRUCTION = """\
=== YOUR TASK ===
Using the trading rules and knowledge above, decide ONE action for the CURRENT bar
from the market state that follows. Reply with EXACTLY one fenced json block and
nothing else:

```json
{"action": "ENTER_LONG|ENTER_SHORT|EXIT|WAIT",
 "confidence": 0.0-1.0,
 "qty": <int contracts>,
 "stop_ticks": <int or null>,
 "target_ticks": <int or null>,
 "rationale": "<one short sentence>"}
```
Most bars are WAIT — only act on a clean setup. If unsure, choose WAIT. The bridge
re-checks every order against the hard risk limits and may clamp or reject it.
"""

# Context files concatenated into the system prompt, in priority order.
_CONTEXT_ORDER = [
    "HERMES.md", "strategy.md", "order-flow.md", "price-action.md",
    "risk-management.md", "daily-goal.md",
]


def load_context_files(context_dir: str, order: list[str] | None = None) -> str:
    """Concatenate the *.md context files in priority order into one string."""
    order = order or _CONTEXT_ORDER
    d = Path(context_dir)
    if not d.is_dir():
        return ""
    parts: list[str] = []
    for name in order:
        f = d / name
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    for f in sorted(d.glob("*.md")):
        if f.name not in order and f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def build_user_prompt(req: AgentRequest) -> str:
    """Frame the current market state as the agent's user message."""
    bars = [
        {"ts": b.ts, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
        for b in req.recent_bars[-30:]
    ]
    payload = {
        "mode": req.mode,
        "instrument": req.account.instrument,
        "timeframe": req.account.timeframe,
        "context": req.context.to_dict(),
        "account": req.account.model_dump(),
        "recent_bars": bars,
    }
    return "CURRENT MARKET STATE:\n" + json.dumps(payload, separators=(",", ":"))


class HermesAgentClient(AgentClient):
    """Adapter over the installed Hermes runtime (Nous Research hermes-agent).

    Verified against hermes-agent 0.16.0: `run_agent.AIAgent(...)` exposes
    `run_conversation(user_message, system_message=...) -> {"final_response": str}`.
    The agent is constructed ONCE and reused. The trading knowledge is loaded from the
    `context_dir` *.md files into the system prompt so the agent reliably trades the
    configured way. Any failure degrades to WAIT (never auto-trades on error).
    """

    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._agent = None          # cached AIAgent (lazy; needs a configured provider)
        self._system: str | None = None  # cached system prompt

    def decide(self, req: AgentRequest) -> Decision:
        try:
            reply = self._ask(self._system_prompt(), self._user_prompt(req))
            return self._parse(reply)
        except Exception as exc:  # noqa: BLE001 — fail safe: never auto-trade on error
            return Decision(action=Action.WAIT, rationale=f"hermes_error:{type(exc).__name__}")

    # ---- prompt framing -----------------------------------------------------
    def _system_prompt(self) -> str:
        if self._system is not None:
            return self._system
        knowledge = self._load_context_files()
        if not knowledge:
            knowledge = self.cfg.agent.hermes.context_hint
        self._system = f"{knowledge}\n\n{DECISION_INSTRUCTION}"
        return self._system

    def _load_context_files(self) -> str:
        return load_context_files(self.cfg.agent.hermes.context_dir)

    def _user_prompt(self, req: AgentRequest) -> str:
        return build_user_prompt(req)

    # ---- runtime call -------------------------------------------------------
    def _ask(self, system: str, user: str) -> str:
        if self.cfg.agent.hermes.mode == "cli":
            return self._ask_cli(system, user)
        return self._ask_in_process(system, user)

    def _ask_in_process(self, system: str, user: str) -> str:
        agent = self._get_agent()
        result = agent.run_conversation(user, system_message=system)
        if isinstance(result, dict):
            return str(result.get("final_response", ""))
        return str(result)

    def _get_agent(self):
        if self._agent is None:
            # Lazy import so the bridge has no hard dependency on Hermes being present.
            from run_agent import AIAgent  # type: ignore

            h = self.cfg.agent.hermes
            self._agent = AIAgent(
                model=h.model or "",
                enabled_toolsets=list(h.enabled_toolsets),
                skip_memory=h.skip_memory,
                quiet_mode=h.quiet_mode,
            )
        return self._agent

    def _ask_cli(self, system: str, user: str) -> str:
        # Hermes oneshot: `hermes -z "<prompt>"`. Reuses Hermes' own provider/auth
        # resolution (its OAuth logins), unlike a bare in-process AIAgent() construction.
        h = self.cfg.agent.hermes
        out = subprocess.run(
            [h.hermes_bin, "-z", f"{system}\n\n{user}"],
            capture_output=True, text=True, timeout=h.timeout_s,
        )
        return out.stdout

    # ---- response parsing ---------------------------------------------------
    @staticmethod
    def _parse(reply: str) -> Decision:
        block = _extract_json(reply)
        if block is None:
            return Decision(action=Action.WAIT, rationale="no_json_in_reply")
        try:
            data = json.loads(block)
            return Decision.model_validate(data)
        except Exception:  # noqa: BLE001
            return Decision(action=Action.WAIT, rationale="unparseable_decision")


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> str | None:
    m = _JSON_FENCE.search(text)
    if m:
        return m.group(1)
    # Fallback: first balanced-looking object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


def build_agent_client(config: BridgeConfig) -> AgentClient:
    if config.agent.client == "hermes":
        return HermesAgentClient(config)
    if config.agent.client == "claude":
        from .claude_agent import ClaudeAgentClient  # lazy: avoid circular import
        return ClaudeAgentClient(config)
    return MockAgentClient(config)
