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


def test_panel_txt(cfg):
    c = _client(cfg)
    bars = synthetic_bars(60)
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    c.post("/ingest/bar", json={"instrument": "ES", "timeframe": "5m",
                                "bar": bars[-1].model_dump()})
    r = c.get("/panel.txt")
    assert r.status_code == 200
    lines = r.text.splitlines()
    kv = dict(line.split("=", 1) for line in lines
              if "=" in line and not line.startswith("row="))
    assert kv["ok"] == "1"
    assert kv["instrument"] == "ES" and kv["timeframe"] == "5m"
    assert kv["goal_target"] == "500" and kv["goal_loss"] == "400"
    assert "model" in kv and "ld_action" in kv and "ld_rationale" in kv
    # Recent decisions come through as pipe-separated row= lines.
    rows = [line for line in lines if line.startswith("row=")]
    assert rows and len(rows[0].split("=", 1)[1].split("|")) == 5
    # No plan in the payload yet -> no plan_* keys (they appear after the armed-plan merge).
    assert not any(k.startswith("plan_") for k in kv)


def test_ingest_bar_requests_history_when_store_thin(cfg):
    c = _client(cfg)
    bars = synthetic_bars(60)
    # No history pushed yet: a lone live bar must come back flagged.
    r = c.post("/ingest/bar", json={"instrument": "ES", "timeframe": "5m",
                                    "bar": bars[-1].model_dump()})
    assert r.json()["need_history"] is True
    # Once history lands (60 >= HISTORY_MIN_BARS) the flag clears.
    c.post("/ingest/history", json={"instrument": "ES", "timeframe": "5m",
                                    "bars": [b.model_dump() for b in bars]})
    r2 = c.post("/ingest/bar", json={"instrument": "ES", "timeframe": "5m",
                                     "bar": bars[-1].model_dump()})
    assert r2.json()["need_history"] is False


def test_render_panel_price_precision():
    # :g would truncate to 6 significant digits ("21512.8") — a tick+ off at MNQ levels.
    from hermes_bridge.dashboard import render_panel

    d = {
        "instrument": "MNQ", "timeframe": "2m", "agent": "claude", "model": "sonnet",
        "strategy_id": "s", "data_age_seconds": 3.0,
        "last_bar": {"ts": 1.0, "close": 21512.75},
        "session": {"position": -1, "avg_price": 21510.25, "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0, "trades_today": 1, "halted": False,
                    "halt_reason": "", "daily_goal_hit": False},
        "goal": {"profit_target": 500.0, "max_daily_loss": 400.0},
        "last_decision": {"ts": 1.0, "close": 21512.75, "action": "WAIT",
                          "confidence": 0.5, "rationale": "x", "queued": None},
        "recent_decisions": [{"ts": 1.0, "close": 21512.75, "action": "WAIT",
                              "confidence": 0.5, "queued": None}],
    }
    txt = render_panel(d)
    assert "last_close=21512.75" in txt
    assert "avg_price=21510.25" in txt
    assert "|21512.75|" in txt


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


def test_decisions_ring_snapshot_is_thread_safe(cfg):
    # The dashboard snapshots st.decisions (list(deque)) on one worker thread while
    # /ingest/bar appends on another. Both take st.decisions_lock, so the snapshot can't
    # raise "deque mutated during iteration". Mirror that contention here: a writer hammers
    # the ring (under the lock, like ingest_bar) while we build the dashboard payload — which
    # must never raise. Drop either lock and this fails (flakily) with a RuntimeError.
    import threading

    from hermes_bridge.views import build_dashboard_payload

    st = create_app(cfg).state.appstate
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            with st.decisions_lock:
                st.decisions.append({"ts": i, "action": "WAIT"})
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(2000):
            try:
                build_dashboard_payload(st)
            except BaseException as exc:  # noqa: BLE001 — record any concurrency failure
                errors.append(exc)
                break
    finally:
        stop.set()
        t.join(timeout=2)
    assert not errors, f"dashboard snapshot raced with the writer: {errors[0]!r}"
