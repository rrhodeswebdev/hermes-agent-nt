"""Configuration loading and validation.

Reads `config/trading.yaml` into typed Pydantic models. Every field has a safe
default so the bridge (and the test-suite) can run without a config file. Env
vars `HERMES_BRIDGE_HOST` / `HERMES_BRIDGE_PORT` / `HERMES_BRIDGE_AGENT` override
the matching settings.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


def timeframe_seconds(tf: str, default: float = 60.0) -> float:
    """Parse a timeframe like '1m' / '30s' / '1h' / '1d' into seconds; default on bad input."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(tf).lower())
    if not m:
        return default
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


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


class HermesClientConfig(BaseModel):
    # `mode` selects how HermesAgentClient reaches the runtime.
    mode: str = "in_process"            # in_process | cli
    # OpenRouter-style "provider/model"; "" → use Hermes' own config.yaml model.default.
    model: str = ""
    # Directory of *.md context files loaded verbatim into the system prompt (this is
    # how the agent learns the strategy/order-flow/risk/goal). Absolute or relative to CWD.
    context_dir: str = "hermes/context"
    # Toolsets exposed to the agent. [] = pure reasoning (the bridge executes orders);
    # e.g. ["ninjatrader"] to let the agent call the nt_* tools itself.
    enabled_toolsets: list[str] = Field(default_factory=list)
    skip_memory: bool = True           # per-bar decisions are stateless
    quiet_mode: bool = True
    timeout_s: float = 60.0
    # CLI mode (mode == "cli"): shell out to Hermes' non-interactive oneshot
    # (`hermes -z "<prompt>"`). This reuses Hermes' full provider/auth resolution —
    # including its OAuth logins — so it works where a bare in-process AIAgent() does
    # not. Recommended for OAuth-based providers.
    hermes_bin: str = "hermes"         # path to the `hermes` launcher
    # Used only if context_dir is missing/empty.
    context_hint: str = (
        "You are Hermes, a disciplined futures day-trader. Trade a trend-pullback "
        "strategy with order-flow confirmation, ATR brackets, and strict risk limits."
    )


class ClaudeClientConfig(BaseModel):
    # The brain via the Claude Code CLI in headless print mode, on your subscription.
    claude_bin: str = "claude"          # path/name of the claude launcher
    model: str = "sonnet"               # sonnet | haiku | opus | full model id
    safe_mode: bool = True              # --safe-mode: isolate from CLAUDE.md/hooks/MCP
    timeout_s: float = 30.0
    extra_args: list[str] = Field(default_factory=list)  # appended verbatim to the claude argv
    # Trading knowledge loaded verbatim into the system prompt (relative to CWD or absolute).
    context_dir: str = "hermes/context"
    # Used only if context_dir is missing/empty.
    context_hint: str = (
        "You are a disciplined futures day-trader. Trade a trend-pullback strategy "
        "with order-flow confirmation, ATR brackets, and strict risk limits."
    )


class AgentConfig(BaseModel):
    client: str = "mock"              # mock | hermes | claude
    prefilter: str = "none"           # none | mock (mock rules screen entries before Claude)
    hermes: HermesClientConfig = Field(default_factory=HermesClientConfig)
    claude: ClaudeClientConfig = Field(default_factory=ClaudeClientConfig)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787


class ExecutionConfig(BaseModel):
    # Hard gate on real-money trading. Must be explicitly true AND acknowledged.
    allow_live: bool = False
    account: str = "Sim101"
    # 0 = auto (one bar interval). Drops an ENTRY whose decision took >= this many seconds
    # as stale (the bar it reasoned about is old). EXIT/FLATTEN always execute.
    entry_freshness_s: float = 0.0


class LearningConfig(BaseModel):
    enabled: bool = True
    learned_dir: str = "hermes/learned"          # trader-profile.md, agent-notes.md, lessons/*.md
    journal_path: str = "bridge/state/journal.jsonl"  # episodic record of closed trades
    retrieve_k: int = 3                           # similar past trades fed into each decision
    profile_char_limit: int = 1400
    notes_char_limit: int = 2200
    lessons_char_limit: int = 2500
    reflect_enabled: bool = True
    reflect_on_trade_close: bool = True
    reflect_model: str = "sonnet"     # model for reflection/curation calls
    reflect_recent: int = 20          # recent trades shown to reflection for context
    max_lessons: int = 40             # cap applied lessons per reflection


class BridgeConfig(BaseModel):
    strategy_id: str = "hermes-default"
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    strategy: StrategyParams = Field(default_factory=StrategyParams)
    risk: RiskParams = Field(default_factory=RiskParams)
    daily_goal: DailyGoal = Field(default_factory=DailyGoal)
    session: SessionWindow = Field(default_factory=SessionWindow)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)

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


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` onto `base` (override wins; nested dicts merged)."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load config from YAML; fall back to all-defaults if `path` is None/missing.

    If a sibling `*.local.yaml` exists next to `path` (e.g. `config/trading.local.yaml`),
    it is deep-merged ON TOP of the base file. Keep personal values (account, daily risk)
    in that gitignored local file so they never get committed; the base file stays a
    neutral, shareable template.
    """
    if path is None:
        return BridgeConfig().apply_env()
    p = Path(path)
    if not p.exists():
        return BridgeConfig().apply_env()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    local = p.with_name(f"{p.stem}.local{p.suffix}")
    if local.exists():
        overrides = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        data = _deep_merge(data, overrides)
    return BridgeConfig.model_validate(data).apply_env()
