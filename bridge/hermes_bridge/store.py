"""In-memory bar store.

Holds the rolling history the agent reviews. Deliberately simple (a bounded
deque) so it has no external dependencies; swap in SQLite/Parquet later behind the
same interface if you need durability across restarts.
"""

from __future__ import annotations

import threading
from collections import deque

from .models import Bar


class BarStore:
    def __init__(self, instrument: str, timeframe: str, maxlen: int = 5000) -> None:
        self.instrument = instrument
        self.timeframe = timeframe
        self._bars: deque[Bar] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def replace_history(self, bars: list[Bar]) -> int:
        """Bulk-load historical bars (called once when NinjaTrader goes realtime)."""
        with self._lock:
            self._bars.clear()
            for b in bars:
                self._bars.append(b)
            return len(self._bars)

    def append(self, bar: Bar) -> None:
        """Append one new closed bar. De-dupes on identical trailing timestamp."""
        with self._lock:
            if self._bars and self._bars[-1].ts == bar.ts:
                self._bars[-1] = bar  # update in place (bar re-sent)
            else:
                self._bars.append(bar)

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
