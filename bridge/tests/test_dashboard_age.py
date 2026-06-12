from fastapi.testclient import TestClient

from hermes_bridge.config import BridgeConfig
from hermes_bridge.server import create_app


def _client(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.reflect_enabled = False
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    return TestClient(create_app(cfg))


def test_data_age_uses_arrival_time_not_bar_ts(tmp_path):
    c = _client(tmp_path)
    future_ts = 99999999999.0  # bar stamped far in the future (simulates a timezone skew)
    c.post("/ingest/bar", json={
        "instrument": "MNQ", "timeframe": "1m",
        "bar": {"ts": future_ts, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    })
    d = c.get("/dashboard").json()
    # bar.ts itself is untouched (the HUD still shows the strategy's stamp / Eastern chart time)
    assert d["last_bar"]["ts"] == future_ts
    # but data age is measured from server arrival (true UTC) -> small, not a huge skew
    assert d["data_age_seconds"] is not None
    assert 0 <= d["data_age_seconds"] < 60


def test_data_age_none_before_any_bar(tmp_path):
    c = _client(tmp_path)
    assert c.get("/dashboard").json()["data_age_seconds"] is None
