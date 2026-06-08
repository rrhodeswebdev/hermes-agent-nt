"""Configuration loading and validation.

Reads `config/trading.yaml` into typed Pydantic models. Every field has a safe
default so the bridge (and the test-suite) can run without a config file. Env
vars `HERMES_BRIDGE_HOST` / `HERMES_BRIDGE_PORT` / `HERMES_BRIDGE_AGENT` override
the matching settings.
"""

from __future__ import annotations

import os
from pathlib import Path

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
    # including OpenAI Codex OAuth — so it works where a bare in-process AIAgent() does
    # not. Recommended for OAuth providers (Codex, Nous Portal, etc.).
    hermes_bin: str = "hermes"         # path to the `hermes` launcher
    # Used only if context_dir is missing/empty.
    context_hint: str = (
        "You are Hermes, a disciplined futures day-trader. Trade a trend-pullback "
        "strategy with order-flow confirmation, ATR brackets, and strict risk limits."
    )


class AgentConfig(BaseModel):
    client: str = "mock"              # mock | hermes
    hermes: HermesClientConfig = Field(default_factory=HermesClientConfig)


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
