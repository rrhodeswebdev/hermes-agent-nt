from hermes_bridge.config import timeframe_seconds
from hermes_bridge.models import Action
from hermes_bridge.server import is_stale_entry


def test_timeframe_seconds():
    assert timeframe_seconds("1m") == 60
    assert timeframe_seconds("5m") == 300
    assert timeframe_seconds("30s") == 30
    assert timeframe_seconds("1h") == 3600
    assert timeframe_seconds("garbage") == 60.0  # fallback default


def test_is_stale_entry_drops_slow_entries():
    assert is_stale_entry(Action.ENTER_LONG, 65.0, 60.0) is True
    assert is_stale_entry(Action.ENTER_SHORT, 60.0, 60.0) is True  # >= budget
    assert is_stale_entry(Action.ENTER_LONG, 30.0, 60.0) is False


def test_is_stale_entry_never_blocks_exits():
    assert is_stale_entry(Action.EXIT, 999.0, 60.0) is False
    assert is_stale_entry(Action.FLATTEN, 999.0, 60.0) is False


def test_is_stale_entry_zero_budget_disabled():
    assert is_stale_entry(Action.ENTER_LONG, 999.0, 0.0) is False
