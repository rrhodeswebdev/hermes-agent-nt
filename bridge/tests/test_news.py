"""Major-news blackout guard: parsing, the ±window check, and fail-open behavior.

All hermetic — the network fetch (`_fetch_raw`) is stubbed; no real HTTP is made.
"""

from hermes_bridge.config import BridgeConfig, NewsConfig
from hermes_bridge.news import NewsEvent, NewsGuard, _parse_events, _parse_forexfactory

# Mimics the ForexFactory feed shape: title, country (currency), date (ISO+offset), impact.
SAMPLE = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-06-15T08:30:00-04:00", "impact": "High"},
    {"title": "Retail Sales", "country": "USD", "date": "2026-06-15T08:30:00-04:00",
     "impact": "Medium"},
    {"title": "ECB Press Conf", "country": "EUR", "date": "2026-06-15T08:30:00-04:00",
     "impact": "High"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-06-15T00:00:00-04:00",
     "impact": "Holiday"},
    {"title": "Garbage", "country": "USD", "date": "not-a-date", "impact": "High"},
]


def _guard(**news) -> NewsGuard:
    return NewsGuard(BridgeConfig(news=NewsConfig(enabled=True, **news)))


# --- parsing --------------------------------------------------------------- #
def test_parse_keeps_only_matching_impact_and_currency():
    evs = _parse_events(SAMPLE, ["High"], ["USD"])
    # Medium dropped, EUR dropped, Holiday dropped, unparseable date skipped.
    assert [e.title for e in evs] == ["CPI m/m"]
    assert evs[0].ts > 0 and evs[0].currency == "USD" and evs[0].impact == "High"


def test_parse_impacts_and_currencies_are_configurable():
    evs = _parse_events(SAMPLE, ["High", "Medium"], ["USD", "EUR"])
    assert {e.title for e in evs} == {"CPI m/m", "Retail Sales", "ECB Press Conf"}


def test_parse_is_case_insensitive():
    evs = _parse_events(SAMPLE, ["high"], ["usd"])
    assert [e.title for e in evs] == ["CPI m/m"]


# --- blackout window ------------------------------------------------------- #
def test_blackout_active_within_window():
    guard = _guard(window_minutes=2.0)
    t = 1_700_000_000.0
    guard._events = [NewsEvent("CPI", "USD", "High", t)]
    assert guard.blackout_at(t) is not None          # exactly on the event
    assert guard.blackout_at(t + 60) is not None      # 1 min after → blocked
    assert guard.blackout_at(t - 119) is not None      # ~2 min before → blocked
    assert guard.blackout_at(t + 121) is None          # >2 min after → clear
    assert guard.blackout_at(t + 3600) is None         # far away → clear


def test_blackout_none_when_disabled():
    guard = NewsGuard(BridgeConfig(news=NewsConfig(enabled=False, window_minutes=2.0)))
    guard._events = [NewsEvent("CPI", "USD", "High", 1_700_000_000.0)]
    assert guard.blackout_at(1_700_000_000.0) is None


def test_blackout_none_when_timestamp_missing():
    guard = _guard()
    guard._events = [NewsEvent("CPI", "USD", "High", 1_700_000_000.0)]
    assert guard.blackout_at(None) is None


def test_blackout_picks_nearest_event():
    guard = _guard(window_minutes=10.0)
    t = 1_700_000_000.0
    guard._events = [NewsEvent("Far", "USD", "High", t + 300),
                     NewsEvent("Near", "USD", "High", t + 30)]
    ev = guard.blackout_at(t)
    assert ev is not None and ev.title == "Near"


# --- refresh / fail-open --------------------------------------------------- #
def test_refresh_disabled_is_noop():
    guard = NewsGuard(BridgeConfig(news=NewsConfig(enabled=False)))
    assert guard.refresh(0.0) is False
    assert guard._events == []


def test_refresh_success_replaces_cache(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "_fetch_raw", lambda: SAMPLE)
    assert guard.refresh(123.0) is True
    assert [e.title for e in guard._events] == ["CPI m/m"]
    assert guard._last_ok_ts == 123.0 and guard._error is None


def test_refresh_failure_keeps_cache_and_fails_open(monkeypatch):
    guard = _guard(window_minutes=2.0)
    cached = [NewsEvent("Cached CPI", "USD", "High", 1_700_000_000.0)]
    guard._events = list(cached)

    def boom():
        raise OSError("network down")

    monkeypatch.setattr(guard, "_fetch_raw", boom)
    assert guard.refresh(200.0) is False
    assert guard._events == cached          # last-good cache retained
    assert guard._error is not None
    assert guard._last_ok_ts is None        # never succeeded
    # fail-open still protects from the cached calendar.
    assert guard.blackout_at(1_700_000_000.0) is not None


def test_maybe_refresh_respects_cadence(monkeypatch):
    guard = _guard(refresh_minutes=30.0)
    monkeypatch.setattr(guard, "_fetch_raw", lambda: SAMPLE)
    assert guard.maybe_refresh(0.0) is True            # never fetched → due
    assert guard.maybe_refresh(60.0) is False           # 1 min later → not due
    assert guard.maybe_refresh(0.0 + 30 * 60) is True   # cadence elapsed → due


def test_status_shape(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "_fetch_raw", lambda: SAMPLE)
    guard.refresh(123.0)
    s = guard.status(now_ts=0.0)
    assert s["enabled"] is True and s["ok"] is True
    assert s["event_count"] == 1 and s["blackout_active"] is False
    assert set(s) >= {"enabled", "ok", "error", "last_fetch_ts", "event_count",
                      "blackout_active", "active_event", "next_event"}


# --- ForexFactory direct-scrape source ------------------------------------- #
# A trimmed FF calendar page. The real page embeds the calendarComponentStates blob TWICE
# (states [1] and [2] with the same event ids), so the fixture does too — the parser must
# dedupe by id. The `<\/span>` in the day label lives OUTSIDE the events array (never json'd).
_FF_DAY = """{"date":"Sun <span>Jun 14<\\/span>","dateline":1781409600,"add":"","events":[
    {"id":1,"currency":"USD","name":"Core CPI m/m","impactName":"high","dateline":1781476200},
    {"id":2,"currency":"USD","name":"Retail Sales m/m","impactName":"medium","dateline":1781476200},
    {"id":3,"currency":"EUR","name":"ECB Speaks","impactName":"high","dateline":1781476200}
  ]},
  {"date":"Mon <span>Jun 15<\\/span>","dateline":1781496000,"add":"","events":[]}"""
FF_HTML = f"""<!doctype html><html><body>
<script>
window.calendarComponentStates[1] = {{ days: [ {_FF_DAY} ]}};
window.calendarComponentStates[2] = {{ days: [ {_FF_DAY} ]}};
</script></body></html>"""


def test_parse_forexfactory_keeps_only_matching():
    evs = _parse_forexfactory(FF_HTML, ["High"], ["USD"])
    # medium dropped, EUR dropped, empty day skipped, and the double-embed deduped to one.
    assert [e.title for e in evs] == ["Core CPI m/m"]
    assert evs[0].ts == 1781476200.0          # FF dateline is already an epoch
    assert evs[0].currency == "USD" and evs[0].impact == "High"


def test_parse_forexfactory_dedupes_double_embed():
    # The fixture embeds the calendar twice; without dedup this would be 4, not 2.
    evs = _parse_forexfactory(FF_HTML, ["High", "Medium"], ["USD", "EUR"])
    assert len(evs) == 3
    assert sorted(e.title for e in evs) == ["Core CPI m/m", "ECB Speaks", "Retail Sales m/m"]


def test_parse_forexfactory_currency_and_impact_filter():
    evs = _parse_forexfactory(FF_HTML, ["High"], ["USD", "EUR"])
    assert {e.title for e in evs} == {"Core CPI m/m", "ECB Speaks"}


def test_parse_forexfactory_is_robust_to_garbage():
    assert _parse_forexfactory("<html>no calendar</html>", ["High"], ["USD"]) == []
    assert _parse_forexfactory('"events":[{bad json', ["High"], ["USD"]) == []  # no closing ]


def test_refresh_uses_forexfactory_when_source_set(monkeypatch):
    guard = NewsGuard(BridgeConfig(news=NewsConfig(enabled=True, source="forexfactory")))
    monkeypatch.setattr(guard, "_fetch_html", lambda: FF_HTML)
    assert guard.refresh(500.0) is True
    assert [e.title for e in guard._events] == ["Core CPI m/m"]
    assert guard._last_ok_ts == 500.0 and guard._error is None
