from pathlib import Path

from hermes_bridge.agent_client import _CONTEXT_ORDER, load_context_files
from hermes_bridge.config import StrategyParams
from hermes_bridge.dashboard import DASHBOARD_HTML, render_text
from hermes_bridge.indicators import (
    absorption,
    build_context,
    depth_imbalance,
    liquidity_walls,
    spread_and_top,
)
from hermes_bridge.models import Bar, DepthLevel, DepthSnapshot
from hermes_bridge.resample import aggregate_bars

_CONTEXT_DIR = str(Path(__file__).resolve().parents[2] / "hermes" / "context")


def test_bar_parses_without_depth():
    b = Bar.model_validate(
        {"ts": 1.0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
    )
    assert b.depth is None


def test_bar_parses_with_depth_wire_shape():
    # The exact JSON shape the C# strategy appends on the realtime bar overload.
    wire = {
        "ts": 1.0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
        "is_closed": True,
        "depth": {
            "bids": [{"price": 100.0, "size": 5}, {"price": 99.75, "size": 8}],
            "asks": [{"price": 100.25, "size": 3}, {"price": 100.5, "size": 12}],
        },
    }
    b = Bar.model_validate(wire)
    assert b.depth is not None
    assert b.depth.bids[0] == DepthLevel(price=100.0, size=5)
    assert b.depth.asks[0].price == 100.25
    # Round-trips back to the same shape.
    assert b.model_dump()["depth"]["asks"][1]["size"] == 12


def test_depth_snapshot_defaults_empty():
    s = DepthSnapshot()
    assert s.bids == [] and s.asks == []


def _snap(bids, asks):
    return DepthSnapshot(
        bids=[DepthLevel(price=p, size=s) for p, s in bids],
        asks=[DepthLevel(price=p, size=s) for p, s in asks],
    )


def test_depth_imbalance_bid_heavy():
    s = _snap([(100, 30), (99.75, 20)], [(100.25, 5), (100.5, 5)])
    # (50 - 10) / (50 + 10) = 0.666...
    assert round(depth_imbalance(s, levels=5), 3) == 0.667


def test_depth_imbalance_empty_book_is_zero():
    assert depth_imbalance(DepthSnapshot(), levels=5) == 0.0


def test_liquidity_walls_flags_outsized_level():
    # Mean size = (2+2+2+18)/4 = 6; wall threshold 3x = 18 -> only the 18 qualifies.
    s = _snap([(100, 2), (99.75, 2)], [(100.25, 2), (100.5, 18)])
    walls = liquidity_walls(s, wall_multiple=3.0)
    assert walls == [(100.5, 18, "ask")]


def test_spread_and_top():
    s = _snap([(100, 7)], [(100.25, 4)])
    spread, tb, ta = spread_and_top(s)
    assert round(spread, 2) == 0.25 and tb == 7 and ta == 4


def test_spread_and_top_one_sided():
    spread, tb, ta = spread_and_top(_snap([(100, 7)], []))
    assert spread is None and tb == 7 and ta is None


def _bar(low, high, snap):
    return Bar(ts=1.0, open=low, high=high, low=low, close=high, volume=1, depth=snap)


def test_absorption_detects_held_bid_wall():
    # A 30-lot bid wall at 100.0 that price dips to on 2 of the recent bars -> support absorbed.
    wall = _snap([(100.0, 30), (99.75, 2)], [(100.25, 2), (100.5, 2)])
    bars = [
        _bar(low=100.0, high=101.0, snap=wall),
        _bar(low=100.5, high=101.5, snap=wall),
        _bar(low=100.0, high=100.9, snap=wall),
    ]
    assert absorption(bars, wall_multiple=3.0, min_tests=2) == "bid_absorption@100"


def test_absorption_none_when_no_wall():
    flat = _snap([(100.0, 2)], [(100.25, 2)])
    assert absorption([_bar(100.0, 101.0, flat)]) is None


def test_absorption_none_without_depth():
    assert absorption([Bar(ts=1.0, open=1, high=2, low=0.5, close=1.5, volume=1)]) is None


def test_depth_knobs_default():
    sp = StrategyParams()
    assert sp.depth_imbalance_levels == 5
    assert sp.depth_wall_multiple == 3.0


def test_depth_knobs_override():
    sp = StrategyParams(depth_imbalance_levels=8, depth_wall_multiple=4.0)
    assert sp.depth_imbalance_levels == 8 and sp.depth_wall_multiple == 4.0


def test_to_dict_unchanged_when_depth_absent():
    # Regression guard: an L1 context emits no depth keys.
    bars = [
        Bar(ts=float(i), open=100, high=101, low=99, close=100.5, volume=10)
        for i in range(30)
    ]
    d = build_context(bars, atr_period=14).to_dict()
    for k in (
        "depth_imbalance",
        "spread",
        "depth_walls",
        "top_bid_size",
        "top_ask_size",
        "absorption",
    ):
        assert k not in d


def test_build_context_populates_depth_when_present():
    snap = _snap([(100.0, 30), (99.75, 20)], [(100.25, 5), (100.5, 5)])
    bars = [
        Bar(ts=float(i), open=100, high=101, low=99, close=100.5, volume=10)
        for i in range(29)
    ]
    bars.append(Bar(ts=29.0, open=100, high=101, low=99, close=100.5, volume=10, depth=snap))
    d = build_context(bars, atr_period=14, imbalance_levels=5, wall_multiple=3.0).to_dict()
    assert round(d["depth_imbalance"], 3) == 0.667
    assert round(d["spread"], 2) == 0.25
    assert d["top_bid_size"] == 30 and d["top_ask_size"] == 5


def test_market_depth_in_context_order_after_order_flow():
    assert "market-depth.md" in _CONTEXT_ORDER
    assert _CONTEXT_ORDER.index("market-depth.md") == _CONTEXT_ORDER.index("order-flow.md") + 1


def test_market_depth_loaded_into_prompt():
    text = load_context_files(_CONTEXT_DIR)
    assert "depth_imbalance" in text  # the guidance references the feature the brain receives


def _text_payload(depth: dict | None) -> dict:
    # render_text indexes several other top-level/session/goal keys directly (not .get), so
    # a bare {"depth": ...} payload KeyErrors before it ever reaches the ladder. Shape a
    # minimal-but-complete payload, matching the pattern in test_dashboard_news.py's _payload.
    return {
        "agent": "mock", "brain": "rules", "instrument": "MNQ", "timeframe": "1m",
        "session": {"position": 0, "avg_price": 0.0, "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0, "trades_today": 0, "halted": False,
                    "halt_reason": None},
        "goal": {"profit_target": 500.0, "max_daily_loss": 400.0},
        "depth": depth,
    }


def test_render_text_shows_ladder_when_depth_present():
    payload = _text_payload({
        "bids": [[100.0, 30], [99.75, 20]],
        "asks": [[100.25, 5], [100.5, 5]],
        "imbalance": 0.667, "spread": 0.25, "walls": [], "absorption": None,
    })
    out = render_text(payload)
    assert "DOM" in out or "book" in out.lower()
    assert "100.25" in out  # an ask price is rendered


def test_render_text_no_ladder_when_depth_absent():
    out = render_text(_text_payload(None))
    assert "100.25" not in out


def test_dashboard_js_dom_join_uses_escaped_newline():
    # Regression: DASHBOARD_HTML is a non-raw triple-quoted string, so the embedded
    # JS must use '\\n' (backslash-n survives to the browser) — a bare '\n' collapses
    # to a raw line terminator and is a JS SyntaxError that breaks the whole <script>.
    assert r"rows.join('\n')" in DASHBOARD_HTML


def test_aggregate_carries_last_feed_bars_depth():
    snap1 = _snap([(100.0, 5)], [(100.25, 3)])
    snap2 = _snap([(100.0, 30), (99.75, 20)], [(100.25, 5)])
    b1 = Bar(ts=60.0, open=100, high=101, low=99, close=100.5, volume=5, depth=snap1)
    b2 = Bar(ts=120.0, open=100.5, high=101.5, low=100, close=101, volume=7, depth=snap2)
    agg = aggregate_bars([b1, b2])
    # The decision bar closes on b2, so it carries b2's book (mirrors close=bars[-1].close).
    assert agg.depth == snap2
    assert agg.close == 101  # sanity: aggregation still closes on the last bar


def test_aggregate_depth_none_when_feed_bars_have_none():
    b1 = Bar(ts=60.0, open=100, high=101, low=99, close=100.5, volume=5)
    b2 = Bar(ts=120.0, open=100.5, high=101.5, low=100, close=101, volume=7)
    assert aggregate_bars([b1, b2]).depth is None
