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
