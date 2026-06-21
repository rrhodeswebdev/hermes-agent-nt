"""The news-blackout status is rendered in the text panel, the key=value panel, and the
HTML dashboard (the JSON already carries it; these assert the human-visible surfaces)."""

from hermes_bridge.dashboard import DASHBOARD_HTML, render_panel, render_text


def _payload(news: dict) -> dict:
    return {
        "agent": "mock", "brain": "rules", "model": "", "strategy_id": "test",
        "strategy_source": "agent", "strategy": {}, "account": "Sim101",
        "instrument": "MNQ", "timeframe": "1m", "now": 1000.0,
        "last_bar": {"ts": 1000.0, "close": 21500.0}, "data_age_seconds": 3.0,
        "session": {"position": 0, "avg_price": 0.0, "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0, "trades_today": 0, "halted": False,
                    "halt_reason": None, "daily_goal_hit": False},
        "goal": {"profit_target": 500.0, "max_daily_loss": 400.0},
        "stale_drops": 0, "last_decision": None, "recent_decisions": [],
        "planner": None, "levels": {}, "news": news,
    }


CLEAR = {"enabled": True, "ok": True, "error": None, "last_fetch_ts": 1.0,
         "event_count": 3, "blackout_active": False, "active_event": None,
         "next_event": {"title": "Federal Funds Rate", "currency": "USD", "ts": 2000.0}}
BLACKOUT = {"enabled": True, "ok": True, "error": None, "last_fetch_ts": 1.0,
            "event_count": 3, "blackout_active": True,
            "active_event": "USD:High:CPI m/m", "next_event": None}
DOWN = {"enabled": True, "ok": False, "error": "OSError: down", "last_fetch_ts": None,
        "event_count": 0, "blackout_active": False, "active_event": None, "next_event": None}
DISABLED = {"enabled": False, "ok": False, "error": None, "last_fetch_ts": None,
            "event_count": 0, "blackout_active": False, "active_event": None, "next_event": None}


# --- render_text ----------------------------------------------------------- #
def test_text_shows_blackout():
    out = render_text(_payload(BLACKOUT))
    assert "news: BLACKOUT" in out and "CPI m/m" in out


def test_text_shows_clear_with_next_event():
    out = render_text(_payload(CLEAR))
    assert "news: clear" in out and "Federal Funds Rate" in out


def test_text_shows_feed_down():
    out = render_text(_payload(DOWN))
    assert "news: feed down (trading)" in out


def test_text_omits_news_when_disabled():
    assert "news:" not in render_text(_payload(DISABLED))


# --- render_panel ---------------------------------------------------------- #
def test_panel_emits_blackout_keys():
    out = render_panel(_payload(BLACKOUT))
    assert "news_enabled=1" in out
    assert "news_blackout=1" in out
    assert "news_active=USD:High:CPI m/m" in out


def test_panel_clear_has_next_and_no_blackout():
    out = render_panel(_payload(CLEAR))
    assert "news_blackout=0" in out
    assert "news_next=Federal Funds Rate" in out


def test_panel_omits_news_when_disabled():
    assert "news_enabled" not in render_panel(_payload(DISABLED))


# --- HTML ------------------------------------------------------------------ #
def test_html_has_news_element():
    assert 'id="news"' in DASHBOARD_HTML and 'id="nstatus"' in DASHBOARD_HTML


# --- entry-window posture (item 3) ----------------------------------------- #
def test_panel_emits_entry_window():
    d = _payload(CLEAR)
    d["entry_window"] = "WIND_DOWN"
    assert "entry_window=WIND_DOWN" in render_panel(d)


def test_text_shows_entry_window():
    d = _payload(CLEAR)
    d["entry_window"] = "HALTED"
    assert "entry: HALTED" in render_text(d)


def test_panel_entry_window_blank_when_absent():
    # No entry_window key (e.g. no bar yet) -> the key still emits, empty (dumb C# parser).
    assert "entry_window=" in render_panel(_payload(CLEAR))


def test_html_has_entry_window_pill():
    assert 'id="ewin"' in DASHBOARD_HTML


# --- brain health (DOWN / THROTTLED visible at a glance) -------------------- #
def test_panel_emits_brain_status():
    d = _payload(CLEAR)
    d["brain_status"] = "DOWN"
    assert "brain_status=DOWN" in render_panel(d)


def test_panel_brain_status_blank_when_absent():
    # Key always emits (dumb C# parser), empty when there is no status yet.
    assert "brain_status=" in render_panel(_payload(CLEAR))


def test_text_shows_brain_status_when_not_ok():
    d = _payload(CLEAR)
    d["brain_status"] = "THROTTLED"
    assert "brain: THROTTLED" in render_text(d)


def test_text_omits_brain_status_when_ok():
    # A healthy brain is the common case — don't clutter the panel with "brain: OK".
    d = _payload(CLEAR)
    d["brain_status"] = "OK"
    assert "brain:" not in render_text(d)


def test_html_has_brain_status_pill():
    assert 'id="bhealth"' in DASHBOARD_HTML
