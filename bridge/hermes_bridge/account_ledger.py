"""Account-lifetime equity ledger for prop-firm trailing-drawdown (MLL) enforcement + risk scaling.

The bridge's :class:`SessionState` is DAY-scoped — realized P&L, trade count and halt state reset
every CME trading day. A prop firm's **Max Loss Limit** (trailing drawdown / MLL) is
ACCOUNT-lifetime: it trails the account's *end-of-day* high balance up, then LOCKS once the account
clears its initial trail balance, and it never resets daily nor moves back down (see
``hermes/prop-firms/lucid.md``). This ledger holds the account-level state the RiskGate needs to
enforce that limit and to scale per-trade risk to the *remaining* drawdown room.

It persists across BOTH a restart AND a trading-day roll (unlike the day accounting) to a JSON
sidecar and, when a bars_db is configured, a single-row ``account_ledger`` table — written to both,
read db-first. All persistence is best-effort: a write/read failure never breaks the trading path.

Pure data + small helpers, so the math is unit-tested without standing up the server.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS account_ledger ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "initial_balance REAL NOT NULL, "
        "mll_amount REAL, "
        "cumulative_realized REAL NOT NULL, "
        "eod_high_balance REAL NOT NULL, "
        "last_folded_day INTEGER)"
    )
    # Migrate a ledger table created before last_folded_day existed (added 2026-07-10 for the
    # boot-time weekend-gap fold guard) so an existing deployment reads/writes without a reset.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(account_ledger)").fetchall()}
    if "last_folded_day" not in cols:
        conn.execute("ALTER TABLE account_ledger ADD COLUMN last_folded_day INTEGER")


@dataclass
class AccountLedger:
    """Account-lifetime equity + trailing-drawdown state.

    ``initial_balance`` — the account starting balance (e.g. 50,000).
    ``mll_amount`` — the trailing drawdown in USD (e.g. 2,000); ``None`` disables the MLL.
    ``cumulative_realized`` — sum of ALL PRIOR trading days' realized P&L, net of commission.
    ``eod_high_balance`` — the highest end-of-day balance ever seen (defaults to
      ``initial_balance``); ratchets UP only, at the day roll.
    ``last_folded_day`` — the CME trading day most recently folded into the totals; the fold
      guard keys on it so a day banked by the live roll can't be re-banked by a boot-time gap
      fold (or vice versa). ``None`` until the first keyed fold.
    """

    initial_balance: float
    mll_amount: float | None = None
    cumulative_realized: float = 0.0
    eod_high_balance: float | None = None
    last_folded_day: int | None = None

    def __post_init__(self) -> None:
        self.initial_balance = float(self.initial_balance)
        if self.mll_amount is not None:
            self.mll_amount = float(self.mll_amount)
        self.cumulative_realized = float(self.cumulative_realized)
        if self.eod_high_balance is None:
            self.eod_high_balance = self.initial_balance
        else:
            self.eod_high_balance = float(self.eod_high_balance)
        if self.last_folded_day is not None:
            self.last_folded_day = int(self.last_folded_day)

    # ---- MLL math -----------------------------------------------------------
    def mll_floor(self) -> float | None:
        """The trailing Max-Loss floor: trails the EOD high up, LOCKS at the initial balance
        (never rises above the start, never drops). ``None`` when the MLL is disabled."""
        if self.mll_amount is None:
            return None
        return min(self.eod_high_balance - self.mll_amount, self.initial_balance)

    def balance(self, today_realized_net: float = 0.0) -> float:
        """Realized account balance = start + all prior days' net + today's realized net."""
        return self.initial_balance + self.cumulative_realized + today_realized_net

    def equity(self, today_realized_net: float = 0.0, unrealized: float = 0.0) -> float:
        """Live equity including the open position — the value the intraday MLL breach checks."""
        return self.balance(today_realized_net) + unrealized

    def mll_room(self, today_realized_net: float = 0.0, unrealized: float = 0.0) -> float | None:
        """USD of live equity above the MLL floor. ``None`` when disabled; may go negative (a
        breach) — the RiskGate halts on ``<= 0``."""
        floor = self.mll_floor()
        if floor is None:
            return None
        return self.equity(today_realized_net, unrealized) - floor

    # ---- day roll -----------------------------------------------------------
    def fold_day(self, day_realized_net: float, day_key: int | None = None) -> bool:
        """At the trading-day roll: bank the closing day's realized net into the lifetime total
        and ratchet the end-of-day high-water-mark UP (never down).

        A day can be closed by either the live roll or — when the bridge was down across the roll
        (e.g. a weekend) — a boot-time gap fold. Pass ``day_key`` (the closing CME trading day) so
        the two paths can't double-bank the same day: a repeat fold of ``last_folded_day`` is a
        no-op. Returns ``True`` when the fold was applied, ``False`` when refused as a duplicate.
        Omitting ``day_key`` keeps the old unconditional behavior (no guard, no record)."""
        if day_key is not None and day_key == self.last_folded_day:
            return False
        self.cumulative_realized += float(day_realized_net)
        eod_balance = self.initial_balance + self.cumulative_realized
        if eod_balance > self.eod_high_balance:
            self.eod_high_balance = eod_balance
        if day_key is not None:
            self.last_folded_day = int(day_key)
        return True

    # ---- persistence --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "initial_balance": self.initial_balance,
            "mll_amount": self.mll_amount,
            "cumulative_realized": self.cumulative_realized,
            "eod_high_balance": self.eod_high_balance,
            "last_folded_day": self.last_folded_day,
        }

    def write(self, json_path: str | None = None, db_path: str | None = None) -> None:
        """Best-effort persist to the JSON sidecar and/or the sqlite ``account_ledger`` table.
        A failure on either path is swallowed — persistence must never break trading."""
        if json_path:
            try:
                p = Path(json_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(self.to_dict()), encoding="utf-8")
            except OSError:
                pass
        if db_path:
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                _ensure_table(conn)
                d = self.to_dict()
                conn.execute(
                    "INSERT INTO account_ledger "
                    "(id, initial_balance, mll_amount, cumulative_realized, eod_high_balance, "
                    "last_folded_day) "
                    "VALUES (1, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "initial_balance=excluded.initial_balance, mll_amount=excluded.mll_amount, "
                    "cumulative_realized=excluded.cumulative_realized, "
                    "eod_high_balance=excluded.eod_high_balance, "
                    "last_folded_day=excluded.last_folded_day",
                    (d["initial_balance"], d["mll_amount"], d["cumulative_realized"],
                     d["eod_high_balance"], d["last_folded_day"]),
                )
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                if conn is not None:
                    conn.close()

    @staticmethod
    def _restore(json_path: str | None, db_path: str | None) -> dict | None:
        """Read the persisted row, db-first (shared source of truth) then the JSON sidecar."""
        if db_path:
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                _ensure_table(conn)
                row = conn.execute(
                    "SELECT initial_balance, mll_amount, cumulative_realized, eod_high_balance, "
                    "last_folded_day FROM account_ledger WHERE id = 1"
                ).fetchone()
                if row is not None:
                    return {
                        "initial_balance": row[0], "mll_amount": row[1],
                        "cumulative_realized": row[2], "eod_high_balance": row[3],
                        "last_folded_day": row[4],
                    }
            except sqlite3.Error:
                pass
            finally:
                if conn is not None:
                    conn.close()
        if json_path:
            try:
                p = Path(json_path)
                if p.is_file():
                    return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return None

    @classmethod
    def read(
        cls, json_path: str | None, db_path: str | None, *,
        initial_balance: float, mll_amount: float | None,
    ) -> AccountLedger:
        """Restore the ledger, or start fresh at ``initial_balance``.

        The CURRENT config (``initial_balance``, ``mll_amount``) always wins over the persisted
        copy so changing the MLL number takes effect; the equity HISTORY (cumulative + EOD high) is
        restored only when the persisted ``initial_balance`` matches (a different account size ⇒ a
        different account ⇒ start fresh, never carry stale history)."""
        ledger = cls(initial_balance=initial_balance, mll_amount=mll_amount)
        d = cls._restore(json_path, db_path)
        if d is not None and float(d.get("initial_balance", -1.0)) == float(initial_balance):
            ledger.cumulative_realized = float(d.get("cumulative_realized", 0.0))
            eh = d.get("eod_high_balance")
            ledger.eod_high_balance = (
                float(eh) if eh is not None else float(initial_balance)
            )
            lfd = d.get("last_folded_day")
            ledger.last_folded_day = int(lfd) if lfd is not None else None
        return ledger
