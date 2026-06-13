"""Configuration loading and validation.

Reads `config/trading.yaml` into typed Pydantic models. Every field has a safe
default so the bridge (and the test-suite) can run without a config file. Env
vars `HERMES_BRIDGE_HOST` / `HERMES_BRIDGE_PORT` / `HERMES_BRIDGE_AGENT` override
the matching settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class InstrumentConfig(BaseModel):
    symbol: str = "ES"
    timeframe: str = "5m"
    tick_size: float = 0.25
    tick_value: float = 12.50  # USD per tick per contract (ES e-mini = $12.50)


class StrategyParams(BaseModel):
    ema_fast: int = 9
    ema_slow: int = 21
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    atr_target_mult: float = 2.0
    pullback_atr: float = 0.5      # how deep a pullback to the fast EMA counts as a setup
    min_confidence: float = 0.55   # engine ignores Decisions below this confidence


class RiskParams(BaseModel):
    max_contracts: int = 2
    max_risk_per_trade: float = 250.0   # USD
    max_trades_per_day: int = 10
    # Injected only if a decision lacks a stop. Kept within max_risk_per_trade for a
    # single contract (16 ticks * $12.50 = $200 < $250) so the safety net is usable.
    default_stop_ticks: int = 16


class DailyGoal(BaseModel):
    profit_target: float = 500.0   # USD — halt new entries for the day when reached
    max_daily_loss: float = 400.0  # USD — flatten + halt when reached (stored positive)


class SessionWindow(BaseModel):
    enforce_hours: bool = False
    start: str = "09:30"
    end: str = "16:00"
    timezone: str = "America/New_York"


class ClaudeClientConfig(BaseModel):
    # The decision brain via the Claude Code CLI in headless print mode, on your
    # subscription (no ANTHROPIC_API_KEY / metered API).
    claude_bin: str = "claude"          # path/name of the claude launcher
    model: str = "sonnet"               # sonnet | haiku | opus | full model id (haiku = fastest)
    safe_mode: bool = True              # --safe-mode: isolate from CLAUDE.md/hooks/MCP/skills
    # Latency lever. Extended-"thinking" tokens dominate decision latency: uncapped, one
    # decision can emit thousands of tokens (~30–50s) and even blow past timeout_s → WAIT.
    # Threaded into the subprocess as MAX_THINKING_TOKENS. 0 = minimal thinking (fastest,
    # ~10s); raise (e.g. 1024) if decisions show a WAIT bias from too little reasoning;
    # None = uncapped (slowest, most deliberation).
    max_thinking_tokens: int | None = 0
    timeout_s: float = 30.0
    # Model for the one-time session study (planner). None = use `model`. The study
    # reads a long history once and writes the brief the fast per-bar plans build on,
    # so it can afford a bigger model (e.g. model: haiku, session_model: sonnet).
    session_model: str | None = None
    # Keep one `claude` child alive across requests (system prompt paid once) instead
    # of a fresh process per decision. Falls back to one-shot calls on any session
    # failure. Saves the 1-3s CLI cold start on every analysis.
    persistent: bool = False
    # A persistent session accumulates one conversation turn per analysis, so its
    # context (and therefore latency) grows all session long. Recycle the child after
    # this many turns — one cold start every N analyses instead of latency creeping
    # past the plan budget. None = never recycle.
    max_session_turns: int | None = 40
    extra_args: list[str] = Field(default_factory=list)  # appended verbatim to the claude argv
    # Directory of *.md context files loaded verbatim into the system prompt (this is
    # how the agent learns the strategy/order-flow/risk/goal). Absolute or relative to CWD.
    context_dir: str = "hermes/context"
    # Used only if context_dir is missing/empty.
    context_hint: str = (
        "You are a disciplined futures day-trader. Trade a trend-pullback strategy "
        "with order-flow confirmation, ATR brackets, and strict risk limits."
    )


class AgentConfig(BaseModel):
    # Validated: a stale value (e.g. the legacy "hermes" brain, replaced by "claude")
    # must fail loudly at load — never silently fall back to the mock rules brain,
    # which arms triggers and places orders on its own.
    client: Literal["mock", "claude"] = "mock"
    claude: ClaudeClientConfig = Field(default_factory=ClaudeClientConfig)


class PlannerConfig(BaseModel):
    """The pre-armed plan cycle (plan.py): analysis between bars, instant closes."""

    enabled: bool = True
    # Budgets for the background analyses. These are bridge-side limits (surfaced in
    # dashboard error tags), unrelated to NinjaTrader's HttpTimeoutMs. The plan
    # analysis runs between bars, so it can afford more than the per-bar timeout_s.
    plan_timeout_s: float = 75.0
    session_timeout_s: float = 180.0
    # A plan armed from a bar this many closes old no longer fires (market moved on).
    max_plan_age_bars: int = 2


class LevelsConfig(BaseModel):
    """Swing-pivot S/R detection (levels.py) for `GET /levels` + the plan prompt."""

    enabled: bool = True
    lookback: int = 3        # bars on each side that must be lower/higher to confirm a pivot
    merge_ticks: int = 8     # pivots within this many ticks cluster into one zone
    min_touches: int = 1     # zones with fewer pivots are dropped
    max_levels: int = 12     # strongest-first cap on the returned zones


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787


class ExecutionConfig(BaseModel):
    # Hard gate on real-money trading. Must be explicitly true AND acknowledged.
    allow_live: bool = False
    account: str = "Sim101"


class BridgeConfig(BaseModel):
    strategy_id: str = "hermes-default"
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    strategy: StrategyParams = Field(default_factory=StrategyParams)
    risk: RiskParams = Field(default_factory=RiskParams)
    daily_goal: DailyGoal = Field(default_factory=DailyGoal)
    session: SessionWindow = Field(default_factory=SessionWindow)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    levels: LevelsConfig = Field(default_factory=LevelsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    def apply_env(self) -> BridgeConfig:
        host = os.getenv("HERMES_BRIDGE_HOST")
        port = os.getenv("HERMES_BRIDGE_PORT")
        agent = os.getenv("HERMES_BRIDGE_AGENT")
        if host:
            self.server.host = host
        if port:
            self.server.port = int(port)
        if agent:
            self.agent.client = agent
        return self


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load config from YAML; fall back to all-defaults if `path` is None/missing."""
    if path is None:
        return BridgeConfig().apply_env()
    p = Path(path)
    if not p.exists():
        return BridgeConfig().apply_env()
    data = yaml.safe_load(p.read_text()) or {}
    return BridgeConfig.model_validate(data).apply_env()
