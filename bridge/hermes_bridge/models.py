"""Wire models shared by the bridge, NinjaTrader, and the Hermes tools.

These are the message contract. Keep them stable; both the C# strategy and the
Hermes `nt_*` tools serialize/deserialize against these shapes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# The engine's two decision contexts: hunting an entry vs managing an open position.
Mode = Literal["seek_entry", "manage_position"]


class BrainTimeout(Exception):
    """The decision brain exceeded a bridge-side time budget.

    Raised instead of transport-specific timeout exceptions so client-agnostic code
    (the Planner's dashboard error tags) can name the exceeded budget — these are the
    bridge's own `timeout_s` / `planner.*_timeout_s` settings, NOT NinjaTrader's
    HttpTimeoutMs."""

    def __init__(self, budget_s: float) -> None:
        super().__init__(f"brain timed out after {budget_s:g}s")
        self.budget_s = budget_s


class Action(StrEnum):
    """A trade decision/command verb."""

    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"          # close the current position (any side)
    FLATTEN = "FLATTEN"    # hard close everything (kill switch / goal hit)
    WAIT = "WAIT"          # do nothing this bar


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Bar(BaseModel):
    """A single OHLCV bar. `ts` is epoch seconds (UTC) at bar close."""

    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    is_closed: bool = True
    # Optional order-flow inputs if the data feed provides them.
    bid_volume: float | None = None
    ask_volume: float | None = None


class BarBatch(BaseModel):
    instrument: str
    timeframe: str
    bars: list[Bar]


class BarIngest(BaseModel):
    instrument: str
    timeframe: str
    bar: Bar


class Decision(BaseModel):
    """What the agent (LLM or rules) wants to do on this bar."""

    action: Action = Action.WAIT
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    qty: int = 0
    # Protective bracket expressed in ticks from entry (preferred) or absolute price.
    stop_ticks: int | None = None
    target_ticks: int | None = None
    stop_price: float | None = None
    target_price: float | None = None
    rationale: str = ""
    # Transport metadata, not a trading signal: True while the bridge's bar store is
    # too thin to compute trustworthy context (e.g. the bridge restarted mid-session
    # and never received /ingest/history). NinjaTrader reacts by re-sending history.
    need_history: bool = False


class OrderCommand(BaseModel):
    """A risk-approved instruction for NinjaTrader to execute on the Sim account."""

    id: str
    strategy_id: str
    action: Action
    qty: int = 0
    stop_ticks: int | None = None
    target_ticks: int | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reason: str = ""


class Fill(BaseModel):
    """Execution report sent back from NinjaTrader."""

    order_id: str | None = None
    side: Side
    qty: int
    price: float
    ts: float
    position_after: int = 0           # signed: + long, - short, 0 flat
    realized_pnl_delta: float = 0.0   # realized P&L produced by this fill


class Level(BaseModel):
    """One support/resistance zone from swing-pivot clustering (levels.py).

    Served by `GET /levels` for the chart overlay and fed into the plan-analysis
    prompt; `strength` is the pivot (touch) count, `first_ts`/`end_ts` the span the
    zone has been respected."""

    low: float
    high: float
    strength: int
    first_ts: float
    end_ts: float
    kind: Literal["support", "resistance", "pivot"]


class AccountState(BaseModel):
    instrument: str
    timeframe: str
    position: int = 0                 # signed contracts
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    daily_goal_hit: bool = False
    last_bar_ts: float | None = None
