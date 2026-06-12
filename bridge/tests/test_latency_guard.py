from hermes_bridge.config import BridgeConfig, effective_entry_freshness_s, timeframe_seconds
from hermes_bridge.models import Action, OrderCommand
from hermes_bridge.server import is_stale_entry, should_drop_stale


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


def test_limit_entries_are_never_stale_dropped():
    # A resting limit cannot chase price — staleness does not apply to it.
    lim = OrderCommand(id="x", strategy_id="s", action=Action.ENTER_LONG, limit_price=4000.0)
    assert should_drop_stale(lim, elapsed_s=999.0, budget_s=60.0) is False
    mkt = OrderCommand(id="y", strategy_id="s", action=Action.ENTER_LONG)
    assert should_drop_stale(mkt, elapsed_s=999.0, budget_s=60.0) is True


def test_effective_freshness_auto_floors_at_agent_timeout():
    # Shipped shape: 1m bars + claude timeout 90 must NOT yield a 60s budget that
    # silently drops every entry decided in 60-90s.
    cfg = BridgeConfig()
    cfg.instrument.timeframe = "1m"
    cfg.agent.client = "claude"
    cfg.agent.claude.timeout_s = 90.0
    assert effective_entry_freshness_s(cfg) == 90.0


def test_effective_freshness_auto_uses_bar_interval_for_mock():
    cfg = BridgeConfig()
    cfg.instrument.timeframe = "5m"
    cfg.agent.client = "mock"
    assert effective_entry_freshness_s(cfg) == 300.0


def test_effective_freshness_explicit_value_wins():
    cfg = BridgeConfig()
    cfg.instrument.timeframe = "2m"
    cfg.agent.client = "claude"
    cfg.agent.claude.timeout_s = 115.0
    cfg.execution.entry_freshness_s = 60.0  # operator chose stricter — honored as-is
    assert effective_entry_freshness_s(cfg) == 60.0
