"""Session / day state: position accounting, realized P&L, trade count, daily goal.

This module is the single source of truth for "how are we doing today" and owns
the daily-goal logic (halt on profit target, flatten+halt on max daily loss).

Position accounting is done internally (weighted-average cost) so the same code
drives both the live path (NinjaTrader fills) and the offline replay harness with
no NinjaTrader present. NinjaTrader's reported realized delta is logged as
advisory but not used for halting, keeping one consistent P&L model.

The day's accounting (realized P&L, trade count, halt state) is optionally
persisted to disk on every fill and restored on the first bar of the SAME trading
day, so a mid-day bridge restart doesn't reset the dashboard / daily-loss headroom
to zero. Position is never persisted — a clean restart is flat and NinjaTrader's
fills re-derive it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import AccountState, Fill, Side


@dataclass(frozen=True)
class _DayKey:
    """Identifies the trading day for resets. Epoch-seconds floored to UTC day by
    default; a real deployment can key on the session timezone instead."""

    value: int

    @staticmethod
    def from_ts(ts: float) -> _DayKey:
        return _DayKey(int(ts // 86400))


class SessionState:
    def __init__(
        self,
        instrument: str,
        timeframe: str,
        tick_size: float,
        tick_value: float,
        profit_target: float,
        max_daily_loss: float,
        state_path: str | None = None,
    ) -> None:
        self.instrument = instrument
        self.timeframe = timeframe
        self.tick_size = tick_size
        self.tick_value = tick_value
        # Dollars per 1.0 of price movement per contract.
        self.point_value = tick_value / tick_size if tick_size else 1.0
        self.profit_target = profit_target
        self.max_daily_loss = abs(max_daily_loss)

        self.position: int = 0          # signed contracts
        self.avg_price: float = 0.0
        self.realized_pnl: float = 0.0
        self.trades_today: int = 0
        self.halted: bool = False
        self.halt_reason: str = ""
        self.daily_goal_hit: bool = False
        self.last_bar_ts: float | None = None
        self._day: _DayKey | None = None

        # Day-state persistence (realized P&L + trade count + halt state). Empty path =
        # disabled. The file is loaded now but only APPLIED on the first bar, and only when
        # it belongs to the same trading day (see maybe_roll_day) — so a mid-day restart
        # restores the day, while the next day starts clean.
        self._state_path = state_path or None
        self._pending_restore: dict | None = None
        if self._state_path:
            try:
                p = Path(self._state_path)
                if p.is_file():
                    self._pending_restore = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._pending_restore = None

    # ---- day handling -------------------------------------------------------
    def maybe_roll_day(self, ts: float) -> bool:
        """Reset counters if the bar belongs to a new trading day. Returns True on roll."""
        key = _DayKey.from_ts(ts)
        if self._day is None:
            self._day = key
            # Restore a mid-day restart's accounting — but ONLY for the same trading day
            # (never carry yesterday's P&L into today). Position is not restored: a clean
            # restart is flat and NinjaTrader's fills re-derive it.
            if self._pending_restore is not None:
                if self._pending_restore.get("day") == key.value:
                    self.realized_pnl = float(self._pending_restore.get("realized_pnl", 0.0))
                    self.trades_today = int(self._pending_restore.get("trades_today", 0))
                    self.halted = bool(self._pending_restore.get("halted", False))
                    self.halt_reason = self._pending_restore.get("halt_reason", "") or ""
                    self.daily_goal_hit = bool(
                        self._pending_restore.get("daily_goal_hit", False))
                self._pending_restore = None
            self._persist()
            return False
        if key.value != self._day.value:
            self._day = key
            self.realized_pnl = 0.0
            self.trades_today = 0
            self.halted = False
            self.halt_reason = ""
            self.daily_goal_hit = False
            self._persist()
            return True
        return False

    def _persist(self) -> None:
        """Write the day's accounting so a restart can restore it (best-effort; a write
        failure must never break the trading path). No-op when persistence is disabled."""
        if self._state_path is None:
            return
        try:
            p = Path(self._state_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "day": self._day.value if self._day else None,
                "realized_pnl": self.realized_pnl,
                "trades_today": self.trades_today,
                "halted": self.halted,
                "halt_reason": self.halt_reason,
                "daily_goal_hit": self.daily_goal_hit,
            }), encoding="utf-8")
        except OSError:
            pass

    # ---- fills / accounting -------------------------------------------------
    @staticmethod
    def _signed(fill: Fill) -> int:
        if fill.side == Side.LONG:
            return abs(fill.qty)
        if fill.side == Side.SHORT:
            return -abs(fill.qty)
        return 0

    def apply_fill(self, fill: Fill) -> None:
        signed = self._signed(fill)
        if signed == 0:
            return
        price = fill.price
        opening_from_flat = self.position == 0

        if self.position == 0 or (self.position > 0) == (signed > 0):
            # Opening or adding in the same direction → weighted average cost.
            new_pos = self.position + signed
            self.avg_price = (
                self.avg_price * abs(self.position) + price * abs(signed)
            ) / abs(new_pos)
            self.position = new_pos
        else:
            # Reducing, closing, or flipping → realize P&L on the closed portion.
            closing = min(abs(signed), abs(self.position))
            if self.position > 0:
                self.realized_pnl += (price - self.avg_price) * closing * self.point_value
            else:
                self.realized_pnl += (self.avg_price - price) * closing * self.point_value
            new_pos = self.position + signed
            self.position = new_pos
            if self.position == 0:
                self.avg_price = 0.0
            elif (new_pos > 0) == (signed > 0):
                # Remaining position is in the fill's direction → we flipped past
                # flat; the leftover contracts open at the fill price.
                self.avg_price = price

        if opening_from_flat and self.position != 0:
            self.trades_today += 1
        self._persist()

    # ---- marks / goal -------------------------------------------------------
    def mark_bar(self, ts: float) -> None:
        self.last_bar_ts = ts

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.position == 0:
            return 0.0
        return (mark_price - self.avg_price) * self.position * self.point_value

    def check_daily_goal(self) -> str | None:
        """Evaluate the daily goal against realized P&L. Returns a halt reason if a
        new halt condition just triggered, else None. Idempotent once halted."""
        if self.halted:
            return None
        if self.realized_pnl >= self.profit_target:
            self.daily_goal_hit = True
            self.halt("daily_profit_target")
            return "daily_profit_target"
        if self.realized_pnl <= -self.max_daily_loss:
            self.halt("max_daily_loss")
            return "max_daily_loss"
        return None

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self._persist()

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self._persist()

    @property
    def side(self) -> Side:
        if self.position > 0:
            return Side.LONG
        if self.position < 0:
            return Side.SHORT
        return Side.FLAT

    def account_state(self, mark_price: float | None = None) -> AccountState:
        return AccountState(
            instrument=self.instrument,
            timeframe=self.timeframe,
            position=self.position,
            avg_price=round(self.avg_price, 4),
            realized_pnl=round(self.realized_pnl, 2),
            unrealized_pnl=round(self.unrealized_pnl(mark_price), 2) if mark_price else 0.0,
            trades_today=self.trades_today,
            halted=self.halted,
            halt_reason=self.halt_reason,
            daily_goal_hit=self.daily_goal_hit,
            last_bar_ts=self.last_bar_ts,
        )
