"""The agent's S/R + EMAs are exposed via /dashboard.levels and /levels.txt."""

from fastapi.testclient import TestClient

from hermes_bridge.config import BridgeConfig
from hermes_bridge.server import create_app
from tests.conftest import synthetic_bars


def _client(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.reflect_enabled = False
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    return TestClient(create_app(cfg))


def test_levels_none_before_any_bar(tmp_path):
    c = _client(tmp_path)
    assert c.get("/dashboard").json()["levels"] is None
    assert c.get("/levels.txt").text == ""


def test_levels_exposed_after_bars(tmp_path):
    c = _client(tmp_path)
    for b in synthetic_bars(60):
        c.post("/ingest/bar", json={"instrument": "MNQ", "timeframe": "2m",
                                    "bar": b.model_dump()})
    lv = c.get("/dashboard").json()["levels"]
    assert lv is not None
    # EMAs are always populated once warmed up; swing_high/low keys exist (may be None
    # depending on structure). The /levels.txt endpoint emits key=value lines, no JSON.
    assert lv["ema_fast"] is not None and lv["ema_slow"] is not None
    assert "swing_high" in lv and "swing_low" in lv
    txt = c.get("/levels.txt").text
    assert "ema_fast=" in txt
