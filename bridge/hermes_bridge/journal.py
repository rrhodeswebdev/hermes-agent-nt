"""Episodic trade memory — the journal of closed trades and retrieval over it.

`TradeTracker` is a pure observer of the engine's fills/bars: it captures the
entry context, tracks bar-resolution MAE/MFE while in the position, and emits a
`ClosedTrade` when the position returns to flat. `JournalStore` appends those to a
JSON-lines file. `select_similar` pulls the most relevant past trades for a new
decision. Nothing here executes orders or touches risk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .indicators import MarketContext
from .models import Bar, Side


@dataclass(frozen=True)
class ClosedTrade:
    entry_ts: float
    exit_ts: float
    side: str               # "LONG" | "SHORT"
    qty: int
    entry_price: float
    exit_price: float
    realized_pnl: float
    bars_held: int
    mae: float              # max adverse excursion in points (<= 0)
    mfe: float              # max favorable excursion in points (>= 0)
    trend: str              # regime tag at entry
    entry_context: dict     # MarketContext.to_dict() at entry
    rationale: str          # the decision rationale that opened the trade
    confidence: float = 0.0  # the entry decision's confidence (to study conf vs outcome)

    def to_record(self) -> dict:
        return {
            "entry_ts": self.entry_ts, "exit_ts": self.exit_ts, "side": self.side,
            "qty": self.qty, "entry_price": self.entry_price, "exit_price": self.exit_price,
            "realized_pnl": self.realized_pnl, "bars_held": self.bars_held,
            "mae": self.mae, "mfe": self.mfe, "trend": self.trend,
            "confidence": self.confidence,
            "entry_context": self.entry_context, "rationale": self.rationale,
        }


class TradeTracker:
    """Pure trade-lifecycle observer. No I/O. Emits a ClosedTrade on close."""

    def __init__(self) -> None:
        self._e: dict | None = None  # open-trade accumulator

    def on_entry(self, *, ts: float, side: Side, qty: int, price: float,
                 context: MarketContext, rationale: str, confidence: float = 0.0) -> None:
        self._e = {"ts": ts, "side": side, "qty": qty, "price": price,
                   "context": context, "rationale": rationale, "confidence": confidence,
                   "bars_held": 0, "mfe": 0.0, "mae": 0.0}

    def on_bar(self, bar: Bar) -> None:
        e = self._e
        if e is None:
            return
        e["bars_held"] += 1
        if e["side"] == Side.LONG:
            fav, adv = bar.high - e["price"], bar.low - e["price"]
        else:
            fav, adv = e["price"] - bar.low, e["price"] - bar.high
        e["mfe"] = max(e["mfe"], fav)
        e["mae"] = min(e["mae"], adv)

    def open_excursion(self) -> tuple[float, float] | None:
        """(MAE, MFE) in points for the OPEN trade so far, or None when flat. Lets the
        engine's deterministic trade manager know how far the position has run in our
        favor (MFE) before deciding to arm breakeven / trail the stop."""
        if self._e is None:
            return None
        return self._e["mae"], self._e["mfe"]

    def on_exit(self, *, ts: float, price: float, realized_pnl: float) -> ClosedTrade | None:
        e = self._e
        if e is None:
            return None
        ctx: MarketContext = e["context"]
        trade = ClosedTrade(
            entry_ts=e["ts"], exit_ts=ts, side=str(e["side"]).split(".")[-1], qty=e["qty"],
            entry_price=round(e["price"], 4), exit_price=round(price, 4),
            realized_pnl=round(realized_pnl, 2), bars_held=e["bars_held"],
            mae=round(e["mae"], 4), mfe=round(e["mfe"], 4),
            trend=ctx.trend, entry_context=ctx.to_dict(), rationale=e["rationale"],
            confidence=round(float(e.get("confidence", 0.0)), 3),
        )
        self._e = None
        return trade


class JournalStore:
    """Append-only JSON-lines journal of closed trades."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def append(self, trade: ClosedTrade) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trade.to_record(), separators=(",", ":")) + "\n")

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def recent(self, n: int) -> list[dict]:
        if n <= 0:
            return []
        return self.all()[-n:]


def select_similar(trades: list[dict], ctx: MarketContext, k: int) -> list[dict]:
    """Most relevant past trades for the current context: same regime, most recent."""
    if k <= 0 or not trades:
        return []
    same = [t for t in trades if t.get("trend") == ctx.trend]
    pool = same if same else trades
    return pool[-k:]
