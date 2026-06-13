"""Agent clients — the decision brain behind the engine.

`AgentClient` is the interface the engine calls each bar. Two real implementations:

* `MockAgentClient` — a deterministic order-flow + price-action rule set. It makes
  the WHOLE system runnable and testable with no LLM and no API key, and serves as
  the safe fallback if the LLM brain is unavailable. It is also the "rules gate" half
  of the hybrid engine.

* `ClaudeAgentClient` — delegates judgment to the `claude` CLI in headless print mode
  (on your Claude subscription, no API key). The trading knowledge/strategy/risk/goal
  live in the `context/*.md` files; this client just frames the request and parses a
  JSON Decision back. Any failure degrades to WAIT (never auto-trades on a
  malformed/absent response — open positions remain protected by the resting bracket
  stop in NinjaTrader). It lives in `claude_agent.py` to keep this module import-light.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BridgeConfig
from .indicators import MarketContext
from .models import AccountState, Action, Bar, Decision, Mode

if TYPE_CHECKING:  # plan.py imports this module at runtime; annotations only here
    from .plan import PlanRequest, TradePlan


@dataclass
class AgentRequest:
    mode: Mode
    context: MarketContext
    recent_bars: list[Bar]
    account: AccountState


class AgentClient(ABC):
    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config

    @abstractmethod
    def decide(self, req: AgentRequest) -> Decision: ...

    def describe(self) -> str:
        """Short brain label for the dashboard header (model name or "rules")."""
        return type(self).__name__

    # Planning is optional: a client that doesn't implement it degrades safely to
    # "no plan armed" / "no session brief", and the engine falls back to WAIT.
    def propose_plan(self, preq: PlanRequest) -> TradePlan | None:
        """Between-bars analysis: arm explicit conditions for the NEXT bar close."""
        return None

    def analyze_session(self, preq: PlanRequest, history: list[Bar]) -> str:
        """One-time history study at session start; returns the session brief."""
        return ""


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

    def describe(self) -> str:
        return "rules"

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

    # ---- pre-armed plans (same rule math, expressed as next-close conditions) ----
    def propose_plan(self, preq: PlanRequest) -> TradePlan | None:
        from .plan import EntryTrigger, ExitRule, TradePlan  # lazy: plan.py imports us

        c = preq.context
        if preq.mode == "manage_position":
            pos = preq.assumed_position or preq.account.position
            if c.ema_slow is None or pos == 0:
                return TradePlan(mode="manage_position",
                                 rationale="no structure; hold, bracket protects")
            if pos > 0:
                exit_rule = ExitRule(exit_below=c.ema_slow,
                                     rationale="long invalidated: close through slow EMA")
            else:
                exit_rule = ExitRule(exit_above=c.ema_slow,
                                     rationale="short invalidated: close through slow EMA")
            return TradePlan(mode="manage_position", exit=exit_rule,
                             rationale="exit on trend flip, else let the bracket work")
        if c.ema_fast is None or c.atr is None:
            return TradePlan(mode="seek_entry", rationale="insufficient_history")
        p = self.cfg.strategy
        tol = p.pullback_atr * c.atr
        stop_ticks = self._ticks(c.atr * p.atr_stop_mult)
        target_ticks = self._ticks(c.atr * p.atr_target_mult)
        # The decide() rule "pullback tagged the fast EMA, then closed back beyond it"
        # becomes a close band hugging the fast EMA: a close just beyond it is the
        # resumption at value; a close far beyond is an extended bar (chasing).
        if c.trend == "up" and c.recent_delta >= 0:
            trigger = EntryTrigger(
                direction="long", min_close=c.ema_fast, max_close=c.ema_fast + tol,
                qty=1, stop_ticks=stop_ticks, target_ticks=target_ticks,
                confidence=self._confidence(c, +1),
                rationale="uptrend pullback resuming above fast EMA, +delta",
            )
            return TradePlan(mode="seek_entry", bias="long", triggers=[trigger],
                             rationale="uptrend pullback plan")
        if c.trend == "down" and c.recent_delta <= 0:
            trigger = EntryTrigger(
                direction="short", min_close=c.ema_fast - tol, max_close=c.ema_fast,
                qty=1, stop_ticks=stop_ticks, target_ticks=target_ticks,
                confidence=self._confidence(c, -1),
                rationale="downtrend bounce resuming below fast EMA, -delta",
            )
            return TradePlan(mode="seek_entry", bias="short", triggers=[trigger],
                             rationale="downtrend bounce plan")
        return TradePlan(mode="seek_entry", rationale="no trend/flow alignment; no-trade")

    def analyze_session(self, preq: PlanRequest, history: list[Bar]) -> str:
        c = preq.context
        return (f"rules brief: trend={c.trend} atr={c.atr} ema_fast={c.ema_fast} "
                f"ema_slow={c.ema_slow} swing_high={c.swing_high} "
                f"swing_low={c.swing_low} bars={len(history)}")

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
# Shared LLM-prompt framing (used by ClaudeAgentClient)                       #
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


def load_context_files(context_dir: str) -> str:
    """Concatenate the context *.md files in priority order into one string.

    Top-level files come first (the explicitly ordered ones, then the rest sorted),
    then subdirectory files (the regime playbooks under strategies/) sorted by path —
    the agent is tool-less, so anything the knowledge references must be inlined here.
    UTF-8 explicit so reading does not depend on the platform locale (Windows
    defaults to cp1252 and would crash on the em-dashes/arrows in the notes).
    """
    d = Path(context_dir)
    if not d.is_dir():
        return ""
    parts: list[str] = []
    for name in _CONTEXT_ORDER:
        f = d / name
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    # Any other top-level *.md not in the explicit order.
    for f in sorted(d.glob("*.md")):
        if f.name not in _CONTEXT_ORDER and f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    # Subdirectory files (e.g. strategies/trending/*.md), deterministic path order.
    for f in sorted(d.rglob("*.md"), key=lambda p: p.as_posix()):
        if f.parent != d and f.is_file():
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


def build_agent_client(config: BridgeConfig) -> AgentClient:
    if config.agent.client == "claude":
        from .claude_agent import ClaudeAgentClient  # lazy: avoid circular import
        return ClaudeAgentClient(config)
    return MockAgentClient(config)
