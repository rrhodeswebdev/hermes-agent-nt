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
        # Where the playbook comes from: "custom" (on-disk user playbooks) or "agent"
        # (the brain authors its own). Seeded from config; the server overrides it at
        # runtime from NinjaTrader's UseAgentStrategies toggle (set_strategy_source).
        self._strategy_source: str = config.strategies.source

    @abstractmethod
    def decide(self, req: AgentRequest) -> Decision: ...

    def describe(self) -> str:
        """Short brain label for the dashboard header (model name or "rules")."""
        return type(self).__name__

    # ---- strategy source (custom vs agent-authored) -------------------------
    def set_strategy_source(self, source: str) -> None:
        """Switch the playbook source at runtime. ``source`` is "custom" or "agent";
        anything else is ignored (the current source stays)."""
        if source in ("custom", "agent"):
            self._strategy_source = source

    def strategy_source(self) -> str:
        return self._strategy_source

    def generated_strategy(self) -> str | None:
        """The playbook the agent authored this session, or None (custom mode / not yet
        authored / authoring failed). Overridden by ClaudeAgentClient."""
        return None

    def generated_strategies(self) -> list[dict] | None:
        """The named setups the agent authored this session as ``{name, regime, summary}``
        dicts (the dashboard lists them all), or None. Overridden by ClaudeAgentClient."""
        return None

    def clear_generated_strategy(self) -> None:  # noqa: B027 — intentional no-op default
        """Forget the authored playbook so the system prompt instructs WAIT until a fresh one
        is authored (used by /control/reauthor). Base no-op; overridden by ClaudeAgentClient."""

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
    """Structure-based trend-pullback with order-flow confirmation.

    seek_entry: in a structural up/down trend (higher-highs+higher-lows / lower-highs+
    lower-lows — see ``classify_regime``), take a pullback to the most recent swing that
    defines the trend (the higher-low in an uptrend, the lower-high in a downtrend),
    confirmed by cumulative delta and a same-direction bar close. Stops/targets are
    ATR-based. manage_position: exit early if structure flips against us (or the close
    breaks the protective swing with adverse delta); otherwise let the resting bracket work.
    """

    def describe(self) -> str:
        return "rules"

    def decide(self, req: AgentRequest) -> Decision:
        if req.mode == "manage_position":
            return self._manage(req)
        return self._seek_entry(req)

    def _recent_sr(self, bars: list[Bar]) -> tuple[float, float]:
        """Immediate support/resistance = the lowest low / highest high over the last
        ``2*swing_lookback+1`` bars — the local higher-low / lower-high a pullback returns
        to, near price (the structural stand-in for the old fast-EMA "value" line)."""
        window = bars[-(2 * self.cfg.strategy.swing_lookback + 1):]
        return min(b.low for b in window), max(b.high for b in window)

    def _seek_entry(self, req: AgentRequest) -> Decision:
        c = req.context
        p = self.cfg.strategy
        if c.atr is None or len(req.recent_bars) < 2 or c.regime != "trending":
            return Decision(action=Action.WAIT, rationale="no trending structure")

        last = req.recent_bars[-1]
        atr = c.atr
        tol = p.pullback_atr * atr  # how close to the local swing still counts as a "tag"
        stop_ticks = self._ticks(atr * p.atr_stop_mult)
        target_ticks = self._ticks(atr * p.atr_target_mult)
        support, resistance = self._recent_sr(req.recent_bars)

        # Long: structural uptrend, the bar pulled back to TAG the immediate higher-low
        # support and closed back above it on a bullish bar with non-negative order-flow.
        if (c.trend == "up" and last.low <= support + tol and last.close > support
                and last.close > last.open and c.recent_delta >= 0):
            return Decision(
                action=Action.ENTER_LONG, confidence=self._confidence(c, +1), qty=1,
                stop_ticks=stop_ticks, target_ticks=target_ticks,
                rationale="uptrend pullback held the higher-low, closed back up, +delta",
            )
        # Short: mirror at the immediate lower-high resistance.
        if (c.trend == "down" and last.high >= resistance - tol and last.close < resistance
                and last.close < last.open and c.recent_delta <= 0):
            return Decision(
                action=Action.ENTER_SHORT, confidence=self._confidence(c, -1), qty=1,
                stop_ticks=stop_ticks, target_ticks=target_ticks,
                rationale="downtrend pullback held the lower-high, closed back down, -delta",
            )
        return Decision(action=Action.WAIT, rationale="no setup")

    def _manage(self, req: AgentRequest) -> Decision:
        c = req.context
        pos = req.account.position
        broke_low = c.swing_low is not None and c.last_close < c.swing_low
        broke_high = c.swing_high is not None and c.last_close > c.swing_high
        # Exit early when structure flips against the open position (trend turned, or the
        # close broke the protective swing with adverse delta).
        if pos > 0 and (c.trend == "down" or (c.recent_delta < 0 and broke_low)):
            return Decision(action=Action.EXIT, confidence=0.6,
                            rationale="long invalidated: structure/delta flipped down")
        if pos < 0 and (c.trend == "up" or (c.recent_delta > 0 and broke_high)):
            return Decision(action=Action.EXIT, confidence=0.6,
                            rationale="short invalidated: structure/delta flipped up")
        return Decision(action=Action.WAIT, rationale="hold; bracket protects position")

    # ---- pre-armed plans (same rule math, expressed as next-close conditions) ----
    def propose_plan(self, preq: PlanRequest) -> TradePlan | None:
        from .plan import EntryTrigger, ExitRule, TradePlan  # lazy: plan.py imports us

        c = preq.context
        if preq.mode == "manage_position":
            pos = preq.assumed_position or preq.account.position
            if pos > 0 and c.swing_low is not None:
                exit_rule = ExitRule(exit_below=c.swing_low,
                                     rationale="long invalidated: close below the higher-low")
            elif pos < 0 and c.swing_high is not None:
                exit_rule = ExitRule(exit_above=c.swing_high,
                                     rationale="short invalidated: close above the lower-high")
            else:
                return TradePlan(mode="manage_position",
                                 rationale="no structure; hold, bracket protects")
            return TradePlan(mode="manage_position", exit=exit_rule,
                             rationale="exit on a structural break, else let the bracket work")
        if c.atr is None or c.regime != "trending" or len(preq.recent_bars) < 2:
            return TradePlan(mode="seek_entry", rationale="no trending structure; no-trade")
        p = self.cfg.strategy
        tol = p.pullback_atr * c.atr
        stop_ticks = self._ticks(c.atr * p.atr_stop_mult)
        target_ticks = self._ticks(c.atr * p.atr_target_mult)
        support, resistance = self._recent_sr(preq.recent_bars)
        # The decide() rule "pullback tagged the immediate higher-low, then closed back"
        # becomes a close band hugging that local support (long) / resistance (short).
        if c.trend == "up" and c.recent_delta >= 0:
            trigger = EntryTrigger(
                direction="long", min_close=support, max_close=support + tol,
                qty=1, stop_ticks=stop_ticks, target_ticks=target_ticks,
                confidence=self._confidence(c, +1),
                rationale="uptrend pullback resuming off the higher-low, +delta",
            )
            return TradePlan(mode="seek_entry", bias="long", triggers=[trigger],
                             rationale="uptrend pullback plan")
        if c.trend == "down" and c.recent_delta <= 0:
            trigger = EntryTrigger(
                direction="short", min_close=resistance - tol, max_close=resistance,
                qty=1, stop_ticks=stop_ticks, target_ticks=target_ticks,
                confidence=self._confidence(c, -1),
                rationale="downtrend pullback resuming off the lower-high, -delta",
            )
            return TradePlan(mode="seek_entry", bias="short", triggers=[trigger],
                             rationale="downtrend pullback plan")
        return TradePlan(mode="seek_entry", rationale="no trend/flow alignment; no-trade")

    def analyze_session(self, preq: PlanRequest, history: list[Bar]) -> str:
        c = preq.context
        return (f"rules brief: regime={c.regime} trend={c.trend} atr={c.atr} "
                f"swing_high={c.swing_high} swing_low={c.swing_low} bars={len(history)}")

    def _confidence(self, c: MarketContext, direction: int) -> float:
        score = 0.5
        # Wider swing structure (relative to ATR) = a cleaner, roomier trend.
        if c.swing_high is not None and c.swing_low is not None and c.atr:
            span = abs(c.swing_high - c.swing_low)
            score += min(0.25, span / c.atr * 0.05)
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


def load_context_files(context_dir: str, order: list[str] | None = None,
                       include_subdirs: bool = True) -> str:
    """Concatenate the context *.md files in priority order into one string.

    Top-level files come first (the explicitly ordered ones, then the rest sorted),
    then — unless ``include_subdirs`` is False — the subdirectory files (the regime
    playbooks under strategies/) sorted by path. The agent is tool-less, so anything
    the knowledge references must be inlined here. UTF-8 explicit so reading does not
    depend on the platform locale (Windows defaults to cp1252 and would crash on the
    em-dashes/arrows in the notes).

    ``include_subdirs=False`` loads the FRAMEWORK only (decision flow, regime, order
    flow, risk, goal, hard rules) without the on-disk regime playbooks — used by agent
    mode, which appends its OWN authored playbook instead (see claude_agent.py).
    """
    order = order or _CONTEXT_ORDER
    d = Path(context_dir)
    if not d.is_dir():
        return ""
    parts: list[str] = []
    for name in order:
        f = d / name
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    # Any other top-level *.md not in the explicit order.
    for f in sorted(d.glob("*.md")):
        if f.name not in order and f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    # Subdirectory files (e.g. strategies/trending/*.md), deterministic path order.
    if include_subdirs:
        for f in sorted(d.rglob("*.md"), key=lambda p: p.as_posix()):
            if f.parent != d and f.is_file():
                parts.append(f.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def load_playbook_files(context_dir: str) -> str:
    """Concatenate ONLY the regime playbooks under ``context_dir`` subdirectories
    (e.g. strategies/trending/*.md), deterministic path order — the user's "custom
    strategies". Empty (no subdir files) ⇒ "" so callers can detect an empty custom set.
    """
    d = Path(context_dir)
    if not d.is_dir():
        return ""
    parts = [
        f.read_text(encoding="utf-8")
        for f in sorted(d.rglob("*.md"), key=lambda p: p.as_posix())
        if f.parent != d and f.is_file()
    ]
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
    client = config.agent.client
    if client == "claude":
        from .claude_agent import ClaudeAgentClient  # lazy: avoid circular import
        return ClaudeAgentClient(config)
    if client == "mock":
        return MockAgentClient(config)
    # Config validation already rejects unknown values; this guards the env-override
    # path (HERMES_BRIDGE_AGENT), which assigns after validation. An unknown brain
    # must never silently downgrade to the mock rules brain, which trades on its own.
    raise ValueError(
        f"unknown agent.client {client!r}: expected 'claude' or 'mock' "
        "(the legacy 'hermes' brain was replaced by 'claude')"
    )
