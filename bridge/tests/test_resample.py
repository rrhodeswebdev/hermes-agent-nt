from fastapi.testclient import TestClient

import hermes_bridge.resample as resample_mod
from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.config import BridgeConfig, InstrumentConfig
from hermes_bridge.engine import TradingEngine
from hermes_bridge.models import Bar, BarIngest
from hermes_bridge.resample import (
    Resampler,
    aggregate_bars,
    feed_tf_of,
    resample_series,
    resampler_engaged,
)
from hermes_bridge.risk import RiskGate
from hermes_bridge.server import create_app
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore


def _bar(ts, o, h, low, c, v=100.0, bid=None, ask=None):
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v,
               bid_volume=bid, ask_volume=ask)


def _resampler(decision_timeframe="2m", feed_tf="1m", now_fn=lambda: 0.0):
    return Resampler(
        BarStore("MNQ", feed_tf), BarStore("MNQ", "x", db_path=None),
        feed_tf=feed_tf, decision_timeframe=decision_timeframe, now_fn=now_fn,
    )


def _feed_seq():
    # ts ...980 (forming), ...040 (2m close), ...100 (forming), ...160 (2m close)
    return [_bar(1781599980, 100, 100, 100, 100), _bar(1781600040, 101, 101, 101, 101),
            _bar(1781600100, 102, 102, 102, 102), _bar(1781600160, 103, 103, 103, 103)]


# ---- config fields -------------------------------------------------------
def test_instrument_config_defaults_are_passthrough():
    inst = InstrumentConfig()
    assert inst.feed_timeframe == ""
    assert inst.decision_timeframe == "static"


def test_instrument_config_accepts_resampler_fields():
    inst = InstrumentConfig(feed_timeframe="1m", decision_timeframe="auto")
    assert inst.feed_timeframe == "1m"
    assert inst.decision_timeframe == "auto"


def test_bridge_config_default_instrument_passthrough():
    assert BridgeConfig().instrument.decision_timeframe == "static"


# ---- aggregation + engagement -------------------------------------------
def test_aggregate_bars_ohlcv():
    out = aggregate_bars([
        _bar(60, 10, 12, 9, 11, v=100, bid=40, ask=60),
        _bar(120, 11, 15, 10, 14, v=150, bid=70, ask=80),
    ])
    assert out.ts == 120          # last bar's ts (the close boundary)
    assert out.open == 10         # first open
    assert out.close == 14        # last close
    assert out.high == 15         # max high
    assert out.low == 9           # min low
    assert out.volume == 250      # summed
    assert out.bid_volume == 110  # summed
    assert out.ask_volume == 140


def test_aggregate_bars_bid_ask_none_when_all_missing():
    out = aggregate_bars([_bar(60, 10, 12, 9, 11), _bar(120, 11, 15, 10, 14)])
    assert out.bid_volume is None
    assert out.ask_volume is None


def test_resample_series_folds_on_boundary_drops_partial():
    feed = [_bar(60, 1, 1, 1, 1), _bar(120, 2, 2, 2, 2),
            _bar(180, 3, 3, 3, 3), _bar(240, 4, 4, 4, 4),
            _bar(300, 5, 5, 5, 5)]  # trailing partial (300 % 120 != 0)
    out = resample_series(feed, 120)
    assert [b.ts for b in out] == [120, 240]
    assert out[0].open == 1 and out[0].close == 2
    assert out[1].open == 3 and out[1].close == 4


def test_feed_tf_of_falls_back_to_timeframe():
    assert feed_tf_of(InstrumentConfig(timeframe="1m")) == "1m"
    assert feed_tf_of(InstrumentConfig(timeframe="5m", feed_timeframe="1m")) == "1m"


def test_resampler_engaged_matrix():
    assert resampler_engaged(InstrumentConfig()) is False          # static, feed==timeframe
    assert resampler_engaged(
        InstrumentConfig(feed_timeframe="1m", decision_timeframe="auto")) is True
    assert resampler_engaged(
        InstrumentConfig(feed_timeframe="1m", decision_timeframe="2m")) is True
    assert resampler_engaged(
        InstrumentConfig(feed_timeframe="1m", decision_timeframe="1m")) is False
    assert resampler_engaged(
        InstrumentConfig(timeframe="1m", decision_timeframe="static")) is False


# ---- scheduling + accumulation ------------------------------------------
def test_scheduled_tf_override_is_fixed(monkeypatch):
    r = _resampler(decision_timeframe="2m")
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "ETH")
    assert r.scheduled_tf(123.0) == "2m"   # override wins over session


def test_scheduled_tf_auto_follows_session(monkeypatch):
    r = _resampler(decision_timeframe="auto")
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "RTH")
    assert r.scheduled_tf(1.0) == "2m"
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "ETH")
    assert r.scheduled_tf(1.0) == "1m"


def test_ingest_aggregate_emits_on_boundary():
    r = _resampler(decision_timeframe="2m")        # current_tf == "2m", feed "1m"
    assert r._ingest_aggregate(_bar(60, 10, 12, 9, 11)) is None     # 60 % 120 != 0 (forming)
    out = r._ingest_aggregate(_bar(120, 11, 15, 10, 14))            # 120 % 120 == 0 (close)
    assert out is not None
    assert out.ts == 120 and out.open == 10 and out.close == 14 and out.high == 15


def test_ingest_aggregate_passthrough_when_decision_equals_feed():
    r = _resampler(decision_timeframe="1m")        # current_tf == feed_tf == "1m"
    b = _bar(61, 10, 12, 9, 11)                     # not even on a 60-boundary
    assert r._ingest_aggregate(b) is b              # passthrough, no buffering


# ---- switch + rebuild + history -----------------------------------------
def test_on_feed_bar_emits_only_on_decision_close():
    r = _resampler(decision_timeframe="2m")
    outs = [r.on_feed_bar(b, is_flat=True) for b in _feed_seq()]
    assert [o is None for o in outs] == [True, False, True, False]
    assert len(r.feed_store) == 4
    assert [o.ts for o in outs if o is not None] == [1781600040, 1781600160]


def test_stream_equivalent_to_resample_series():
    feed = _feed_seq()
    r = _resampler(decision_timeframe="2m")
    streamed = [o for b in feed if (o := r.on_feed_bar(b, is_flat=True)) is not None]
    assert [(b.ts, b.open, b.close) for b in streamed] == \
           [(b.ts, b.open, b.close) for b in resample_series(feed, 120)]


def test_switch_defers_until_flat_then_rebuilds(monkeypatch):
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "ETH")
    r = _resampler(decision_timeframe="auto")        # starts current_tf == "1m" (ETH)
    assert r.current_tf == "1m"
    for b in _feed_seq():                             # 1m passthrough into the decision store
        out = r.on_feed_bar(b, is_flat=True)
        if out is not None:
            r.decision_store.append(out)
    # RTH now wants 2m, but a position is open -> defer
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "RTH")
    r.on_feed_bar(_bar(1781600220, 104, 104, 104, 104), is_flat=False)
    assert r.current_tf == "1m"                       # deferred
    # flat again -> switch + lossless rebuild from the feed store
    out = r.on_feed_bar(_bar(1781600280, 105, 105, 105, 105), is_flat=True)
    assert r.current_tf == "2m"
    # the switch bar closes the [220, 280] window: a FULL window (open 104 from the 220 bar),
    # not a lone 280 bar — guards against re-folding the switch bar after the rebuild.
    assert out is not None and out.ts == 1781600280 and out.open == 104 and out.close == 105
    expected = resample_series(r.feed_store.all(), 120)
    assert [b.ts for b in r.decision_store.all()] == [b.ts for b in expected]
    assert r.decision_store.last().open == 104


def test_replace_feed_history_rebuilds_decision_store():
    r = _resampler(decision_timeframe="2m")
    r.replace_feed_history(_feed_seq())
    assert len(r.feed_store) == 4
    assert [b.ts for b in r.decision_store.all()] == [1781600040, 1781600160]


# ---- live TF switch re-author signal ------------------------------------
def test_take_switch_fires_once_on_live_flat_switch(monkeypatch):
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "ETH")
    r = _resampler(decision_timeframe="auto")        # starts current_tf == "1m" (ETH)
    assert r.take_switch() is False                  # nothing switched yet
    for b in _feed_seq():                             # 1m passthrough, no switch
        r.on_feed_bar(b, is_flat=True)
    assert r.take_switch() is False
    # RTH now wants 2m and we are flat -> live switch sets the flag
    monkeypatch.setattr(resample_mod, "session_for_ts", lambda ts: "RTH")
    r.on_feed_bar(_bar(1781600220, 104, 104, 104, 104), is_flat=True)
    assert r.current_tf == "2m"
    assert r.take_switch() is True                    # consumed on read
    assert r.take_switch() is False                   # second call: already consumed


def test_initial_rebuild_and_replace_feed_history_do_not_set_switch():
    r = _resampler(decision_timeframe="2m")
    r.initial_rebuild()
    assert r.take_switch() is False                    # rebuild is not a live switch
    r.replace_feed_history(_feed_seq())
    assert r.take_switch() is False                    # bulk history load is not a live switch


# ---- engine current decision-timeframe getter ---------------------------
def _eng(cfg, decision_tf=None):
    return TradingEngine(
        cfg, BarStore("ES", "5m"), SessionState("ES", "5m", 0.25, 12.5, 500, 400),
        build_agent_client(cfg), RiskGate(cfg), decision_tf=decision_tf,
    )


def test_engine_decision_tf_defaults_to_config(cfg):
    assert _eng(cfg)._decision_tf() == cfg.instrument.timeframe


def test_engine_decision_tf_uses_injected_getter(cfg):
    assert _eng(cfg, decision_tf=lambda: "2m")._decision_tf() == "2m"


def test_engine_reauthor_forces_session_study(cfg):
    eng = _eng(cfg)
    calls: list[dict] = []
    eng._trigger_session_study = lambda bars, *, force, outcome: calls.append(
        {"force": force, "outcome": outcome})
    eng.reauthor(outcome="tf_switch")
    assert calls == [{"force": True, "outcome": "tf_switch"}]


# ---- server ingest routing (engaged vs not) -----------------------------
def _ingest(client, bar):
    payload = BarIngest(instrument="MNQ", timeframe="1m", bar=bar)
    return client.post("/ingest/bar", json=payload.model_dump())


def test_ingest_routes_through_resampler_when_engaged(cfg):
    cfg.instrument.symbol = "MNQ"
    cfg.instrument.feed_timeframe = "1m"
    cfg.instrument.decision_timeframe = "2m"   # fixed override => deterministic, no session dep
    app = create_app(cfg)
    st = app.state.appstate
    client = TestClient(app)

    r0 = _ingest(client, _bar(1781599980, 100, 100, 100, 100))   # forming
    assert "resample:forming" in r0.json()["rationale"]
    assert len(st.store) == 0                                    # engine not advanced

    _ingest(client, _bar(1781600040, 101, 101, 101, 101))        # 2m close -> engine runs
    assert len(st.store) == 1
    assert len(st.feed_store) == 2

    _ingest(client, _bar(1781600100, 102, 102, 102, 102))        # forming
    assert len(st.store) == 1
    _ingest(client, _bar(1781600160, 103, 103, 103, 103))        # 2m close
    assert len(st.store) == 2
    assert len(st.feed_store) == 4

    first = st.store.recent(2)[0]
    assert first.ts == 1781600040 and first.open == 100 and first.close == 101

    # dashboard shows the LIVE decision timeframe (the override), not the static config value
    assert client.get("/dashboard").json()["timeframe"] == "2m"


def test_ingest_unchanged_when_not_engaged(cfg):
    app = create_app(cfg)                  # default cfg: decision_timeframe == "static"
    st = app.state.appstate
    assert st.resampler is None
    assert st.feed_store is None
    client = TestClient(app)
    _ingest(client, _bar(1781600040, 100, 100, 100, 100))
    assert len(st.store) == 1              # engine advanced directly, as today
    assert client.get("/dashboard").json()["timeframe"] == cfg.instrument.timeframe
