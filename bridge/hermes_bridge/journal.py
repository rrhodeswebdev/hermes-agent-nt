"""Episodic trade memory — the journal of closed trades and retrieval over it.

`TradeTracker` is a pure observer of the engine's fills/bars: it captures the
entry context, tracks bar-resolution MAE/MFE while in the position, and emits a
`ClosedTrade` when the position returns to flat. `JournalStore` appends those to a
JSON-lines file. `select_similar` pulls the most relevant past trades for a new
decision. Nothing here executes orders or touches risk.
"""

from __future__ import annotations

import json
import threading
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
    stop_price: float = 0.0   # the trade's original protective stop (0.0 if unattributed)
    target_price: float = 0.0  # the trade's original target (0.0 if unattributed)

    def to_record(self) -> dict:
        return {
            "entry_ts": self.entry_ts, "exit_ts": self.exit_ts, "side": self.side,
            "qty": self.qty, "entry_price": self.entry_price, "exit_price": self.exit_price,
            "realized_pnl": self.realized_pnl, "bars_held": self.bars_held,
            "mae": self.mae, "mfe": self.mfe, "trend": self.trend,
            "confidence": self.confidence,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "entry_context": self.entry_context, "rationale": self.rationale,
        }


class TradeTracker:
    """Pure trade-lifecycle observer. No I/O. Emits a ClosedTrade on close."""

    def __init__(self) -> None:
        self._e: dict | None = None  # open-trade accumulator

    def on_entry(self, *, ts: float, side: Side, qty: int, price: float,
                 context: MarketContext, rationale: str, confidence: float = 0.0,
                 stop_price: float = 0.0, target_price: float = 0.0) -> None:
        self._e = {"ts": ts, "side": side, "qty": qty, "price": price,
                   "context": context, "rationale": rationale, "confidence": confidence,
                   "stop_price": stop_price, "target_price": target_price,
                   "bars_held": 0, "mfe": 0.0, "mae": 0.0}

    def note_scale(self, *, qty: int, avg_price: float) -> None:
        """A later fill grew the OPEN position (a partial entry completing, or pyramiding
        in the same direction). Track the peak size and the volume-weighted average entry,
        so a position built across several fills journals as ONE trade at its FULL size —
        not just the first leg. No-op when flat."""
        if self._e is None:
            return
        self._e["qty"] = qty
        self._e["price"] = avg_price

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
            stop_price=round(float(e.get("stop_price", 0.0)), 4),
            target_price=round(float(e.get("target_price", 0.0)), 4),
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


class DeclineLog:
    """Append-only JSONL of RESOLVED counterfactuals for declined/unfilled setups,
    plus in-memory tracking of would-win outcomes not yet shown to a reflection.

    This is the evidence stream that lets reflection NARROW or RETIRE a lesson that
    over-blocks: vetoed setups never become trades, so without it the learning loop
    could only ever add restrictions, never relax one."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._watermark_path = self.path.with_suffix(".watermark.json")
        self._unreported_wins: list[dict] = []
        # Appends come from the bar-loop thread (engine), snapshots/clears from the
        # reflection trigger paths — guard the in-memory list with its own lock.
        self._lock = threading.Lock()
        # Restart-safe: rebuild the queue from disk (would_win records resolved after
        # the last reported watermark). Runs before any thread touches this instance.
        self._seed_unreported()

    def append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if rec.get("outcome") == "would_win":
            with self._lock:
                self._unreported_wins.append(rec)

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

    def _read_watermark(self) -> float:
        try:
            data = json.loads(self._watermark_path.read_text(encoding="utf-8"))
            return float(data.get("reported_through_ts", 0.0))
        except (OSError, ValueError, TypeError):
            return 0.0  # missing/corrupt -> re-seed everything (duplicate over lost)

    def _write_watermark(self, ts: float) -> None:
        try:
            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._watermark_path.with_name(self._watermark_path.name + ".tmp")
            tmp.write_text(json.dumps({"reported_through_ts": ts}), encoding="utf-8")
            tmp.replace(self._watermark_path)
        except OSError:
            pass  # best-effort: worst case is re-reporting after the next restart

    def _seed_unreported(self) -> None:
        wm = self._read_watermark()
        for rec in self.all():
            if rec.get("outcome") == "would_win" and float(rec.get("resolved_ts") or 0.0) > wm:
                self._unreported_wins.append(rec)

    def unreported_wins(self) -> list[dict]:
        with self._lock:
            return list(self._unreported_wins)

    def clear_unreported(self) -> None:
        self.take_unreported()  # discard == reported: advance the watermark too

    def take_unreported(self) -> list[dict]:
        """Atomic snapshot-and-clear; advances the persisted watermark so a restart
        cannot resurface already-reported wins. A win resolved concurrently lands in
        the NEXT snapshot instead of being cleared unseen."""
        with self._lock:
            out = list(self._unreported_wins)
            self._unreported_wins.clear()
        if out:
            self._write_watermark(max(float(r.get("resolved_ts") or 0.0) for r in out))
        return out


def select_similar(trades: list[dict], ctx: MarketContext, k: int) -> list[dict]:
    """Most relevant past trades for the current context: same regime, most recent."""
    if k <= 0 or not trades:
        return []
    same = [t for t in trades if t.get("trend") == ctx.trend]
    pool = same if same else trades
    return pool[-k:]
