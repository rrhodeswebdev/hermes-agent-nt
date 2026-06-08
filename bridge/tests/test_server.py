from fastapi.testclient import TestClient

from hermes_bridge.server import create_app
from tests.conftest import synthetic_bars


def _client(cfg) -> TestClient:
    return TestClient(create_app(cfg))


def test_health(cfg):
    c = _client(cfg)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_history_and_recent_bars(cfg):
    c = _client(cfg)
    bars = synthetic_bars(60)
    payload = {
        "instrument": "ES", "timeframe": "5m",
        "bars": [b.model_dump() for b in bars],
    }
    r = c.post("/ingest/history", json=payload)
    assert r.json() == {"ok": True, "stored": 60}
    r = c.get("/bars/recent", params={"n": 10})
    assert len(r.json()["bars"]) == 10


def test_ingest_bar_returns_decision(cfg):
    c = _client(cfg)
    bars = synthetic_bars(60)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    nxt = bars[-1]
    r = c.post("/ingest/bar", json={"instrument": "ES", "timeframe": "5m",
                                    "bar": nxt.model_dump()})
    assert r.status_code == 200
    assert "action" in r.json()


def test_agent_command_flow_through_risk_gate(cfg):
    c = _client(cfg)
    # Seed a price so the gate can evaluate; then place a sane order via the agent path.
    bars = synthetic_bars(60)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    c.post("/ingest/bar", json={"instrument": "ES", "timeframe": "5m",
                                "bar": bars[-1].model_dump()})
    r = c.post("/agent/command", json={"action": "ENTER_LONG", "qty": 1, "stop_ticks": 8})
    body = r.json()
    assert body["approved"] is True
    # The approved command is now queued for NinjaTrader to fetch.
    r2 = c.get("/commands/next", params={"strategy_id": cfg.strategy_id})
    cmd = r2.json()["command"]
    assert cmd is not None and cmd["action"] == "ENTER_LONG" and cmd["qty"] == 1
    # Queue now empty.
    nxt = c.get("/commands/next", params={"strategy_id": cfg.strategy_id})
    assert nxt.json()["command"] is None


def test_agent_command_rejected_when_risk_too_high(cfg):
    c = _client(cfg)
    bars = synthetic_bars(60)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    # 40-tick stop = $500 risk > $250 cap → rejected, nothing queued.
    r = c.post("/agent/command", json={"action": "ENTER_LONG", "qty": 1, "stop_ticks": 40})
    assert r.json()["approved"] is False
    nxt = c.get("/commands/next", params={"strategy_id": cfg.strategy_id})
    assert nxt.json()["command"] is None


def test_dashboard_endpoints(cfg):
    c = _client(cfg)
    bars = synthetic_bars(60)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    c.post("/ingest/bar", json={"instrument": "ES", "timeframe": "5m",
                                "bar": bars[-1].model_dump()})
    j = c.get("/dashboard").json()
    assert "session" in j and "recent_decisions" in j
    assert j["data_age_seconds"] is not None
    assert len(j["recent_decisions"]) >= 1 and "action" in j["recent_decisions"][0]
    txt = c.get("/dashboard.txt")
    assert txt.status_code == 200 and "HERMES" in txt.text
    html = c.get("/")
    assert html.status_code == 200 and "Hermes" in html.text


def test_fill_updates_account_and_flatten_kill_switch(cfg):
    c = _client(cfg)
    # Apply an entry fill, then a closing fill in profit.
    c.post("/ingest/fill", json={"side": "LONG", "qty": 1, "price": 4000.0, "ts": 1})
    acct = c.get("/account").json()
    assert acct["position"] == 1 and acct["trades_today"] == 1
    # Kill switch flattens + halts and queues a FLATTEN.
    r = c.post("/control/flatten", json={"reason": "test_kill"})
    assert r.json()["halted"] is True
    cmd = c.get("/commands/next", params={"strategy_id": cfg.strategy_id}).json()["command"]
    assert cmd["action"] == "FLATTEN"
