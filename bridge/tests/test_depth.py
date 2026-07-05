from hermes_bridge.indicators import (
    depth_imbalance,
    liquidity_walls,
    spread_and_top,
)
from hermes_bridge.models import Bar, DepthLevel, DepthSnapshot


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
