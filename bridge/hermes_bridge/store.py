"""Bar store: a bounded in-memory deque with optional SQLite write-through.

The deque stays the hot path (the engine only ever reads recent slices). With a
db_path the store also persists every bar and reloads the tail on startup, so
multi-day history survives a bridge restart and the replay/calibration tooling has
real history to work from. DB failures degrade to memory-only — persistence is
never allowed to break the bar loop.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import deque
from pathlib import Path

from .models import Bar


class BarStore:
    def __init__(self, instrument: str, timeframe: str, maxlen: int = 5000,
                 db_path: str | None = None) -> None:
        self.instrument = instrument
        self.timeframe = timeframe
        self._bars: deque[Bar] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if db_path:
            try:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(db_path, check_same_thread=False)
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS bars ("
                    "instrument TEXT, timeframe TEXT, ts REAL, "
                    "open REAL, high REAL, low REAL, close REAL, volume REAL, "
                    "ask_volume REAL, bid_volume REAL, "
                    "PRIMARY KEY (instrument, timeframe, ts))")
                self._db.commit()
                for row in self._db.execute(
                    "SELECT ts, open, high, low, close, volume, ask_volume, bid_volume "
                    "FROM bars WHERE instrument=? AND timeframe=? "
                    "ORDER BY ts DESC LIMIT ?",
                    (instrument, timeframe, maxlen),
                ).fetchall()[::-1]:
                    self._bars.append(Bar(ts=row[0], open=row[1], high=row[2],
                                          low=row[3], close=row[4], volume=row[5],
                                          ask_volume=row[6], bid_volume=row[7]))
            except (sqlite3.Error, OSError) as e:  # mkdir can raise OSError too
                print(f"[store] bars db unavailable ({e}); running memory-only",
                      flush=True)
                self._db = None

    def _persist(self, bars: list[Bar]) -> None:
        if self._db is None:
            return
        try:
            # COALESCE keeps real bid/ask delta already on a row when a re-sent bar
            # (history pushes carry no delta) would otherwise null it out.
            self._db.executemany(
                "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument, timeframe, ts) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume, "
                "ask_volume=COALESCE(excluded.ask_volume, bars.ask_volume), "
                "bid_volume=COALESCE(excluded.bid_volume, bars.bid_volume)",
                [(self.instrument, self.timeframe, b.ts, b.open, b.high,
                  b.low, b.close, b.volume, b.ask_volume, b.bid_volume)
                 for b in bars])
            self._db.commit()
        except sqlite3.Error as e:
            print(f"[store] bars db write failed ({e}); disabling persistence",
                  flush=True)
            self._db = None

    def replace_history(self, bars: list[Bar]) -> int:
        """Bulk-load historical bars (called when NinjaTrader goes realtime)."""
        with self._lock:
            self._bars.clear()
            for b in bars:
                self._bars.append(b)
            self._persist(list(bars))
            return len(self._bars)

    def append(self, bar: Bar) -> None:
        """Append one new closed bar. De-dupes on identical trailing timestamp."""
        with self._lock:
            if self._bars and self._bars[-1].ts == bar.ts:
                self._bars[-1] = bar  # update in place (bar re-sent)
            else:
                self._bars.append(bar)
            self._persist([bar])

    def recent(self, n: int) -> list[Bar]:
        with self._lock:
            if n <= 0:
                return []
            return list(self._bars)[-n:]

    def all(self) -> list[Bar]:
        with self._lock:
            return list(self._bars)

    def last(self) -> Bar | None:
        with self._lock:
            return self._bars[-1] if self._bars else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._bars)
