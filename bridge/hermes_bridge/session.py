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

from .account_ledger import AccountLedger
from .indicators import cme_trading_day
from .models import AccountState, Fill, Side


@dataclass(frozen=True)
class _DayKey:
    """Identifies the CME trading day for daily-goal / P&L resets. Keyed on the exchange
    trading day (boundary 17:00 ET, the daily settlement) -- NOT UTC midnight -- so an evening
    ETH session (opens 18:00 ET) is a NEW day and never inherits that morning's RTH P&L,
    matching how NT8's daily realized rolls. See indicators.cme_trading_day."""

    value: int

    @staticmethod
    def from_ts(ts: float) -> _DayKey:
        return _DayKey(cme_trading_day(ts))


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
        commission_per_contract: float = 0.0,
        ledger_db_path: str | None = None,
    ) -> None:
        self.instrument = instrument
        self.timeframe = timeframe
        self.tick_size = tick_size
        self.tick_value = tick_value
        # Dollars per 1.0 of price movement per contract.
        self.point_value = tick_value / tick_size if tick_size else 1.0
        self.profit_target = profit_target
        self.max_daily_loss = abs(max_daily_loss)
        self.commission_per_contract = commission_per_contract

        self.position: int = 0          # signed contracts
        # Contracts of an APPROVED entry that NinjaTrader has not reported filled yet.
        # `position` lags the fill, so without this the RiskGate would see a flat book
        # twice and approve two entries whose sum busts the position cap.
        self.pending_entry_qty: int = 0
        self.avg_price: float = 0.0
        # Price of the protective stop currently RESTING in NinjaTrader for the open
        # position, as last approved by the RiskGate (None = only the entry bracket).
        # The gate ratchets against this so a stop can never be moved wider.
        self.working_stop: float | None = None
        self.realized_pnl: float = 0.0
        self.commission_total: float = 0.0
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

        # Account-lifetime ledger for prop-firm trailing-drawdown (MLL) enforcement + risk
        # scaling. None until attach_ledger() is called (by apply_account_profile when
        # enforce_trailing_drawdown is on). Persisted to BOTH a JSON sidecar and the sqlite db,
        # and — unlike the day accounting above — it survives the trading-day roll.
        # The ledger's JSON sidecar sits next to the day-state file (account_ledger.json); the db
        # is the shared bars_db. Either may be None (persistence simply off for that channel).
        self._ledger_json_path = (
            str(Path(self._state_path).with_name("account_ledger.json"))
            if self._state_path else None
        )
        self._ledger_db_path = ledger_db_path or None
        self.ledger: AccountLedger | None = None
        # The account's eval profit target (set with the ledger).
        self.eval_profit_target: float | None = None
        # The funded scaling plan as (min_profit, max_contracts) rungs; None ⇒ no scaling table.
        self.scaling_tiers: list[tuple[float, int]] | None = None

    # ---- day handling -------------------------------------------------------
    def maybe_roll_day(self, ts: float) -> bool:
        """Reset counters if the bar belongs to a new trading day. Returns True on roll."""
        key = _DayKey.from_ts(ts)
        if self._day is None:
            self._day = key
            # Restore a mid-day restart's accounting — but ONLY for the same trading day
            # (never carry yesterday's P&L into today).
            if self._pending_restore is not None:
                if self._pending_restore.get("day") == key.value:
                    self.realized_pnl = float(self._pending_restore.get("realized_pnl", 0.0))
                    self.commission_total = float(
                        self._pending_restore.get("commission_total", 0.0))
                    self.trades_today = int(self._pending_restore.get("trades_today", 0))
                    self.halted = bool(self._pending_restore.get("halted", False))
                    self.halt_reason = self._pending_restore.get("halt_reason", "") or ""
                    self.daily_goal_hit = bool(
                        self._pending_restore.get("daily_goal_hit", False))
                    # Open exposure too. A restart while holding used to come back reading FLAT
                    # while NinjaTrader still held the contracts, so the exit fill was applied as
                    # an OPENING fill and inverted the book into a phantom opposite position
                    # (live 2026-08-09, 4-lot long). Restoring is also the CONSERVATIVE error:
                    # if NT8 actually flattened while we were down, a restored phantom only
                    # BLOCKS new entries (the gate's flat-only check) instead of hiding real
                    # exposure — and the next fill's position_after snaps it straight (apply_fill).
                    self.position = int(self._pending_restore.get("position", 0))
                    self.avg_price = float(self._pending_restore.get("avg_price", 0.0))
                    ws = self._pending_restore.get("working_stop")
                    self.working_stop = float(ws) if ws is not None else None
                else:
                    # A PRIOR trading day's accounting survived on disk because the bridge was
                    # down across that day's roll (e.g. the Fri->Sun weekend), so the live fold
                    # in the roll branch below never ran. Bank that day's realized net into the
                    # account ledger now — same effect as the live roll — so cumulative + the MLL
                    # high-water-mark reflect the banked profit instead of restoring to zero.
                    self._fold_gap_day(self._pending_restore)
                self._pending_restore = None
            self._persist()
            return False
        if key.value != self._day.value:
            closing_day = self._day.value
            self._day = key
            # Bank the CLOSING day's realized net into the account ledger and ratchet its
            # end-of-day high-water-mark BEFORE the day's accounting resets. The ledger is
            # account-lifetime, so it does not reset here. Keyed by the closing day so a boot-time
            # gap fold of the same day can't double-bank it.
            if self.ledger is not None:
                self.ledger.fold_day(self.realized_net, closing_day)
                self._persist_ledger()
            self.realized_pnl = 0.0
            self.commission_total = 0.0
            self.trades_today = 0
            self.halted = False
            self.halt_reason = ""
            self.daily_goal_hit = False
            self._persist()
            return True
        return False

    def _fold_gap_day(self, restore: dict) -> None:
        """Bank a prior trading day's realized net into the account ledger on boot.

        When the bridge is down across a day roll (a weekend), the persisted day-state belongs to
        a CLOSED trading day that the live ``maybe_roll_day`` fold never banked. Fold it here so
        the lifetime cumulative + the MLL high-water-mark match the true banked profit. The
        ledger fold is idempotent per day (keyed on the closing day), so a raced/partial persist
        can't double-count. No-op without a ledger. Best-effort persist, like the live roll."""
        if self.ledger is None:
            return
        realized = float(restore.get("realized_pnl", 0.0))
        commission = float(restore.get("commission_total", 0.0))
        day = restore.get("day")
        day_key = int(day) if day is not None else None
        if self.ledger.fold_day(realized - commission, day_key):
            self._persist_ledger()

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
                "commission_total": self.commission_total,
                "trades_today": self.trades_today,
                "halted": self.halted,
                "halt_reason": self.halt_reason,
                "daily_goal_hit": self.daily_goal_hit,
                # Open exposure, so a restart while HOLDING comes back holding (see
                # maybe_roll_day). working_stop rides along because the RiskGate ratchets
                # against it — restoring it keeps a stop from being widened after a restart.
                "position": self.position,
                "avg_price": self.avg_price,
                "working_stop": self.working_stop,
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
        # Commission accrues on every fill (entry AND exit), per side.
        self.commission_total += abs(signed) * self.commission_per_contract
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
                self.working_stop = None   # nothing rests once the book is flat
            elif (new_pos > 0) == (signed > 0):
                # Remaining position is in the fill's direction → we flipped past
                # flat; the leftover contracts open at the fill price.
                self.avg_price = price

        if opening_from_flat and self.position != 0:
            self.trades_today += 1
        self._reconcile_position(fill)
        self._persist()

    def _reconcile_position(self, fill: Fill) -> None:
        """Snap the book to NinjaTrader's own signed position when it disagrees with ours.

        NT8 stamps every fill with `SignedPosition()` — the real book. Our value is DERIVED by
        summing fills, so it drifts whenever one goes missing: a fill posted while the bridge was
        down, a restart across an exit, a hand-posted resync. Trusting NT8 here makes every such
        drift self-healing on the very next fill instead of compounding silently.

        `position_after=None` means nobody reported it — leave the derived value alone."""
        truth = fill.position_after
        if truth is None or truth == self.position:
            return
        print(f"[session] position reconciled to NinjaTrader: {self.position} -> {truth} "
              f"(fill {fill.side.value} {fill.qty} @ {fill.price})", flush=True)
        if truth == 0:
            self.position = 0
            self.avg_price = 0.0
            self.working_stop = None   # nothing rests once the book is flat
            return
        # Snapping onto real exposure we had not derived (a fill we never saw): this fill's
        # price is the best cost basis available. An existing basis is kept — it came from
        # fills we did see.
        if self.position == 0 or (self.position > 0) != (truth > 0):
            self.avg_price = fill.price
        self.position = truth

    # ---- marks / goal -------------------------------------------------------
    def mark_bar(self, ts: float) -> None:
        self.last_bar_ts = ts

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.position == 0:
            return 0.0
        return (mark_price - self.avg_price) * self.position * self.point_value

    @property
    def realized_net(self) -> float:
        """Realized P&L after deducting all commissions paid this session."""
        return self.realized_pnl - self.commission_total

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

    # ---- account ledger (trailing-drawdown / MLL) ---------------------------
    def attach_ledger(
        self, initial_balance: float, mll_amount: float | None,
        eval_profit_target: float | None = None,
    ) -> None:
        """Build (or restore) the account ledger and persist it. Called by
        ``apply_account_profile`` when ``enforce_trailing_drawdown`` is on."""
        self.ledger = AccountLedger.read(
            self._ledger_json_path, self._ledger_db_path,
            initial_balance=initial_balance, mll_amount=mll_amount,
        )
        self.eval_profit_target = eval_profit_target
        self._persist_ledger()

    def _persist_ledger(self) -> None:
        if self.ledger is not None:
            self.ledger.write(self._ledger_json_path, self._ledger_db_path)

    def mll_floor(self) -> float | None:
        return self.ledger.mll_floor() if self.ledger is not None else None

    def account_equity(self, mark_price: float | None = None) -> float | None:
        """Live account equity: start + lifetime realized + today's realized net + open P&L."""
        if self.ledger is None:
            return None
        unreal = self.unrealized_pnl(mark_price) if mark_price is not None else 0.0
        return self.ledger.equity(self.realized_net, unreal)

    def mll_room(self, mark_price: float | None = None) -> float | None:
        """USD of live equity above the MLL floor (``None`` when no ledger)."""
        if self.ledger is None:
            return None
        unreal = self.unrealized_pnl(mark_price) if mark_price is not None else 0.0
        return self.ledger.mll_room(self.realized_net, unreal)

    def check_mll(self, mark_price: float | None, buffer_usd: float = 0.0) -> str | None:
        """Halt when live equity falls to the trailing MLL floor (+ buffer) — the caller's
        flatten-on-halt path then closes the position. Parallel to ``check_daily_goal``: returns
        the halt reason on a NEW breach, else ``None``. Idempotent once halted; no-op without a
        ledger."""
        if self.halted or self.ledger is None:
            return None
        room = self.mll_room(mark_price)
        if room is not None and room <= buffer_usd:
            self.halt("mll_breached")
            return "mll_breached"
        return None

    @property
    def side(self) -> Side:
        if self.position > 0:
            return Side.LONG
        if self.position < 0:
            return Side.SHORT
        return Side.FLAT

    def account_state(self, mark_price: float | None = None) -> AccountState:
        eq = self.account_equity(mark_price)
        floor = self.mll_floor()
        room = self.mll_room(mark_price)
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
            realized_net=round(self.realized_net, 2),
            commission=round(self.commission_total, 2),
            account_equity=round(eq, 2) if eq is not None else None,
            mll_floor=round(floor, 2) if floor is not None else None,
            mll_room=round(room, 2) if room is not None else None,
        )
