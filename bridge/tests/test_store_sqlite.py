"""SQLite write-through for the bar store (optional, opt-in via db_path).

The deque stays the hot path; with a db_path the store also persists every bar and
reloads the tail on construction, so multi-day history survives a bridge restart.
DB failures must degrade to memory-only — persistence never breaks the bar loop.
"""

from __future__ import annotations

from hermes_bridge.models import Bar
from hermes_bridge.store import BarStore


def _bar(ts: float, close: float, av: float | None = None, bv: float | None = None) -> Bar:
    return Bar(ts=ts, open=close, high=close + 1, low=close - 1, close=close,
               volume=10.0, ask_volume=av, bid_volume=bv)


def test_bars_survive_reopen(tmp_path):
    db = str(tmp_path / "bars.db")
    s1 = BarStore("MNQ", "1m", db_path=db)
    s1.append(_bar(1_700_000_000, 100.0))
    s1.append(_bar(1_700_000_060, 101.0))
    s2 = BarStore("MNQ", "1m", db_path=db)
    assert [b.close for b in s2.recent(10)] == [100.0, 101.0]


def test_history_repush_preserves_real_delta(tmp_path):
    # A re-pushed history bar carries no delta; the COALESCE upsert must keep the real
    # bid/ask delta already stored from the realtime bar rather than nulling it.
    db = str(tmp_path / "bars.db")
    s1 = BarStore("MNQ", "1m", db_path=db)
    s1.append(_bar(1_700_000_000, 100.0, av=7.0, bv=3.0))   # realtime bar with real delta
    s1.replace_history([_bar(1_700_000_000, 100.0)])         # delta-less history re-push
    s2 = BarStore("MNQ", "1m", db_path=db)
    b = s2.recent(1)[0]
    assert b.ask_volume == 7.0
    assert b.bid_volume == 3.0


def test_no_db_path_is_memory_only(tmp_path):
    s = BarStore("MNQ", "1m")  # no db_path → pure in-memory, no file touched
    s.append(_bar(1_700_000_000, 100.0))
    assert len(s.recent(10)) == 1
    assert s._db is None
    assert not list(tmp_path.iterdir())
