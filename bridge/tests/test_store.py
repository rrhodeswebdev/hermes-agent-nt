from hermes_bridge.store import BarStore
from tests.conftest import make_bar


def test_replace_and_recent():
    store = BarStore("ES", "5m")
    bars = [make_bar(i, 1, 2, 0, 1) for i in range(10)]
    assert store.replace_history(bars) == 10
    assert len(store) == 10
    recent = store.recent(3)
    assert [b.ts for b in recent] == [7, 8, 9]


def test_append_and_dedup_same_ts():
    store = BarStore("ES", "5m")
    store.append(make_bar(1, 1, 2, 0, 1))
    store.append(make_bar(2, 1, 2, 0, 1.5))
    assert len(store) == 2
    # Re-send the last bar (same ts) → update in place, no growth.
    store.append(make_bar(2, 1, 3, 0, 2.0))
    assert len(store) == 2
    assert store.last().close == 2.0


def test_maxlen_eviction():
    store = BarStore("ES", "5m", maxlen=5)
    for i in range(10):
        store.append(make_bar(i, 1, 2, 0, 1))
    assert len(store) == 5
    assert store.recent(5)[0].ts == 5
