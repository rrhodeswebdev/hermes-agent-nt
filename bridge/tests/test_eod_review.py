from datetime import UTC, datetime
from unittest.mock import patch

from hermes_bridge.claude_agent import ClaudeAgentClient
from hermes_bridge.config import LearningConfig
from hermes_bridge.journal import ClosedTrade, JournalStore
from hermes_bridge.market_calendar import _et_date
from hermes_bridge.memory import LearnedStore
from hermes_bridge.reflect import Reflector, _rth_window, build_day_digest
from hermes_bridge.server import eod_should_run


def test_eod_config_defaults_are_neutral():
    lc = LearningConfig()
    assert lc.eod_review_enabled is False
    assert lc.eod_review_cutoff_et == "16:05"
    assert lc.day_review_keep == 10
    assert lc.day_lesson_repeat_n == 3
    assert lc.day_lesson_lookback_m == 5


def test_day_review_append_and_read(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.append_day_review("2026-06-29", "Trend grind. No fills.", keep=10)
    s.append_day_review("2026-06-30", "Range day. One short.", keep=10)
    revs = s.day_reviews(10)
    assert [d for d, _ in revs] == ["2026-06-30", "2026-06-29"]   # newest first
    assert "Range day" in revs[0][1]
    assert s.day_reviews_mtime() > 0


def test_day_review_rolling_cap(tmp_path):
    s = LearnedStore(str(tmp_path))
    for i in range(5):
        s.append_day_review(f"2026-06-2{i}", f"review {i}", keep=3)
    revs = s.day_reviews(10)
    assert len(revs) == 3                                          # capped to keep=3
    assert revs[0][0] == "2026-06-24"                             # newest kept


def test_format_for_prompt_includes_day_reviews(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.append_day_review("2026-06-29", "Trend grind, sub-0.50 pullbacks blocked.", keep=10)
    out = s.format_for_prompt(day_reviews_n=3)
    assert "RECENT DAY-REVIEWS" in out
    assert "Trend grind" in out
    # Off by default:
    assert "RECENT DAY-REVIEWS" not in s.format_for_prompt()


def test_learned_block_wires_day_reviews(tmp_path, cfg):
    # The brain's prompt builder must pass day_reviews_n so it reads its own recent day-reviews.
    cfg.learning.enabled = True
    cfg.learning.day_review_keep = 10
    c = ClaudeAgentClient(cfg)
    c._learned = LearnedStore(str(tmp_path))
    c._learned.append_day_review("2026-06-29", "Trend grind day-review body.", keep=10)
    block = c._learned_block()
    assert "RECENT DAY-REVIEWS" in block and "Trend grind" in block


def _rth_ts(now):
    # a timestamp inside today's RTH window: use 14:00 ET-ish by anchoring on now.
    return now - 3600


def test_build_day_digest_counts_and_window():
    # 2026-06-29 is EDT (UTC-4).  RTH window = 13:30Z – 20:00Z.
    # Use a concrete "now" well inside RTH: 14:00 ET = 18:00 UTC.
    now = datetime(2026, 6, 29, 18, 0, tzinfo=UTC).timestamp()

    # In-window decline: 14:30 ET = 18:30 UTC
    inwin = datetime(2026, 6, 29, 18, 30, tzinfo=UTC).timestamp()
    # Out-of-window decline: 08:00 ET = 12:00 UTC (before 13:30Z open)
    pre_open = datetime(2026, 6, 29, 12, 0, tzinfo=UTC).timestamp()
    # Also a 12:00 ET = 16:00 UTC decline (inside window)
    midday = datetime(2026, 6, 29, 16, 0, tzinfo=UTC).timestamp()
    # In-window trade (exit_ts)
    trade_ts = datetime(2026, 6, 29, 17, 0, tzinfo=UTC).timestamp()

    declines = [
        {"resolved_ts": inwin, "outcome": "would_lose", "suppressed_by": "min_confidence",
         "regime": "trending", "side": "LONG", "confidence": 0.3, "delta_ratio": 0.02,
         "rationale": "pullback buy"},
        {"resolved_ts": midday, "outcome": "would_win", "suppressed_by": "", "regime": "trending",
         "side": "LONG", "confidence": 0.18, "delta_ratio": 0.01, "rationale": "shelf hold"},
        # 08:00 ET = 12:00 UTC — before RTH open (13:30Z on EDT date): must be OUT
        {"resolved_ts": pre_open, "outcome": "would_lose", "suppressed_by": "",
         "regime": "ranging", "side": "SHORT", "confidence": 0.6, "delta_ratio": -0.1,
         "rationale": "pre-open, should be excluded"},
    ]
    trade = {
        "exit_ts": trade_ts, "entry_ts": trade_ts - 600, "side": "LONG",
        "realized_pnl": 50.0, "qty": 1, "entry_price": 100.0, "exit_price": 101.0,
    }
    pa = {"open": 29300.0, "high": 30060.0, "low": 29299.0, "close": 30000.0,
          "range": 761.0, "bars": 390}
    d = build_day_digest(now, declines, pa, trades=[trade], session={"realized_pnl": -30.0})
    assert d["declines"]["total"] == 2                       # the pre-open one is out of window
    assert d["declines"]["by_outcome"] == {"would_lose": 1, "would_win": 1}
    assert d["declines"]["by_suppressed"]["min_confidence"] == 1
    assert d["pa"]["range"] == 761.0
    assert d["trades"]["count"] == 1                         # trade with exit_ts in window
    assert d["trades"]["pnl"] == 50.0


def test_build_day_digest_caps_items():
    now = 1_782_750_000.0
    declines = [{"resolved_ts": now - 3600, "outcome": "would_lose", "suppressed_by": "",
                 "regime": "trending", "side": "LONG", "confidence": 0.3, "delta_ratio": 0.0,
                 "rationale": "x"} for _ in range(40)]
    d = build_day_digest(now, declines, {"range": 0.0}, [], {})
    assert d["declines"]["total"] == 40 and len(d["declines"]["items"]) == 20


def _reflector(tmp_path, cfg):
    return Reflector(cfg, LearnedStore(str(tmp_path)), JournalStore(str(tmp_path / "j.jsonl")))


def test_reflect_on_day_writes_review(tmp_path, cfg):
    r = _reflector(tmp_path, cfg)
    digest = {"date": "2026-06-29", "declines": {"total": 11}, "pa": {"range": 761.0}}
    fake = '{"narrative": "Trend grind; pullbacks sub-0.50, correctly blocked.", ' \
           '"theme": "trend_day_pullback_subconf", "observation": "edge gap = momentum entry"}'
    with patch("hermes_bridge.reflect.run_claude_oneshot", return_value=fake):
        out = r.reflect_on_day(digest)
    assert out["written"] == 1 and out["theme"] == "trend_day_pullback_subconf"
    revs = r.learned.day_reviews(5)
    assert revs and revs[0][0] == "2026-06-29" and "Trend grind" in revs[0][1]


def test_reflect_on_day_swallows_failure(tmp_path, cfg):
    r = _reflector(tmp_path, cfg)
    with patch("hermes_bridge.reflect.run_claude_oneshot", side_effect=RuntimeError("boom")):
        out = r.reflect_on_day({"date": "2026-06-29"})
    assert out["written"] == 0 and out["error"] == "RuntimeError"
    assert r.learned.day_reviews(5) == []


def _seed_reviews(store, themes):
    for i, th in enumerate(themes):
        store.append_day_review(f"2026-06-{10 + i}", f"day {i}\n\n_theme: {th}_", keep=50)


def test_promote_fires_on_repeated_theme(tmp_path, cfg):
    r = _reflector(tmp_path, cfg)
    cfg.learning.day_lesson_repeat_n = 3
    cfg.learning.day_lesson_lookback_m = 5
    _seed_reviews(r.learned, ["trend_subconf", "x", "trend_subconf", "y", "trend_subconf"])
    fake = '{"lessons":[{"op":"create","name":"Trend-day momentum entry",' \
           '"body":"On trend grinds, pullback setups come sub-0.50; need a momentum entry."}]}'
    with patch("hermes_bridge.reflect.run_claude_oneshot", return_value=fake):
        out = r.maybe_promote_day_lesson()
    assert out["promoted"] == 1 and out["theme"] == "trend_subconf"
    names = [ls.name for ls in r.learned.lessons()]
    assert "Trend-day momentum entry" in names


def test_promote_silent_without_repeat(tmp_path, cfg):
    r = _reflector(tmp_path, cfg)
    cfg.learning.day_lesson_repeat_n = 3
    _seed_reviews(r.learned, ["a", "b", "c", "trend_subconf", "d"])
    with patch("hermes_bridge.reflect.run_claude_oneshot") as m:
        out = r.maybe_promote_day_lesson()
    assert out["promoted"] == 0 and m.call_count == 0          # no model call on a one-off


def test_eod_should_run_guard():
    # 2026-06-29 is a Monday (EDT = UTC-4).
    # Build UTC timestamps: 16:30 ET = 20:30 UTC, 15:00 ET = 19:00 UTC.
    after = datetime(2026, 6, 29, 20, 30, tzinfo=UTC).timestamp()   # 16:30 ET — past cutoff
    before = datetime(2026, 6, 29, 19, 0, tzinfo=UTC).timestamp()   # 15:00 ET — pre-cutoff
    assert eod_should_run(after, "16:05", last_date=None, enabled=True) is True
    assert eod_should_run(before, "16:05", last_date=None, enabled=True) is False   # pre-cutoff
    assert eod_should_run(after, "16:05", last_date="2020-01-01", enabled=True) is True  # diff date
    assert eod_should_run(after, "16:05", last_date=_et_date(after).isoformat(),
                          enabled=True) is False                                    # already done
    assert eod_should_run(after, "16:05", last_date=None, enabled=False) is False   # gated off


def test_eod_dedupe_by_et_date_not_cme_day():
    # One ET calendar date can span TWO cme_trading_days (the 17:00 ET roll). Dedupe must key on
    # the RTH date, so a post-roll evening tick does NOT re-fire the same day's review.
    afternoon = datetime(2026, 6, 29, 20, 30, tzinfo=UTC).timestamp()  # 16:30 ET (pre-17:00 roll)
    evening = datetime(2026, 6, 30, 0, 30, tzinfo=UTC).timestamp()     # 20:30 ET, same ET date
    stamp = _et_date(afternoon).isoformat()
    assert _et_date(evening).isoformat() == stamp                      # same RTH date
    assert eod_should_run(evening, "16:05", last_date=stamp, enabled=True) is False


# ---------------------------------------------------------------------------
# New tests for the 4 bugs
# ---------------------------------------------------------------------------

def test_rth_window_utc_exact():
    """_rth_window must return UTC epoch seconds for 09:30 ET -> 16:00 ET."""
    # EDT date: 2026-06-29 (UTC-4)
    # 09:30 ET = 13:30 UTC;  16:00 ET = 20:00 UTC
    now_edt = datetime(2026, 6, 29, 15, 0, tzinfo=UTC).timestamp()  # 11:00 ET
    lo, hi = _rth_window(now_edt)
    assert lo == datetime(2026, 6, 29, 13, 30, tzinfo=UTC).timestamp(), (
        f"EDT open: expected 13:30Z got {datetime.fromtimestamp(lo, tz=UTC)}"
    )
    assert hi == datetime(2026, 6, 29, 20, 0, tzinfo=UTC).timestamp(), (
        f"EDT close: expected 20:00Z got {datetime.fromtimestamp(hi, tz=UTC)}"
    )

    # EST date: 2026-01-05 (UTC-5)
    # 09:30 ET = 14:30 UTC;  16:00 ET = 21:00 UTC
    now_est = datetime(2026, 1, 5, 16, 0, tzinfo=UTC).timestamp()  # 11:00 ET
    lo2, hi2 = _rth_window(now_est)
    assert lo2 == datetime(2026, 1, 5, 14, 30, tzinfo=UTC).timestamp(), (
        f"EST open: expected 14:30Z got {datetime.fromtimestamp(lo2, tz=UTC)}"
    )
    assert hi2 == datetime(2026, 1, 5, 21, 0, tzinfo=UTC).timestamp(), (
        f"EST close: expected 21:00Z got {datetime.fromtimestamp(hi2, tz=UTC)}"
    )


def test_build_day_digest_trades_exit_ts():
    """build_day_digest must filter trades by exit_ts, not a missing 'ts' key."""
    # Use EDT date 2026-06-29; RTH window = 13:30Z – 20:00Z
    now = datetime(2026, 6, 29, 18, 0, tzinfo=UTC).timestamp()
    exit_in_window = datetime(2026, 6, 29, 15, 0, tzinfo=UTC).timestamp()

    # Build record the same way ClosedTrade.to_record() would
    trade_rec = ClosedTrade(
        entry_ts=exit_in_window - 300,
        exit_ts=exit_in_window,
        side="LONG",
        qty=1,
        entry_price=100.0,
        exit_price=101.25,
        realized_pnl=25.0,
        bars_held=5,
        mae=-0.5,
        mfe=1.5,
        trend="trending",
        entry_context={},
        rationale="test trade",
        confidence=0.55,
    ).to_record()

    d = build_day_digest(now, [], {}, trades=[trade_rec], session={})
    assert d["trades"]["count"] == 1, (
        f"expected 1 trade in window, got {d['trades']['count']}"
    )
    assert d["trades"]["pnl"] == 25.0


def test_promote_theme_no_substring_collision(tmp_path, cfg):
    """maybe_promote_day_lesson: theme 'trend_day' must NOT match 'trend_day_pullback' reviews."""
    r = _reflector(tmp_path, cfg)
    cfg.learning.day_lesson_repeat_n = 3
    cfg.learning.day_lesson_lookback_m = 20

    # Seed 4× trend_day (wins the count) and 3× trend_day_pullback.
    # Only the trend_day reviews must appear in the promotion prompt; the
    # trend_day_pullback reviews must be excluded by exact-match comparison.
    _seed_reviews(r.learned, [
        "trend_day", "trend_day_pullback", "trend_day", "trend_day_pullback",
        "trend_day", "trend_day_pullback", "trend_day",
    ])

    captured_users: list[str] = []

    def fake_oneshot(_claude, _system, user, **_kw):
        captured_users.append(user)
        return '{"lessons":[]}'

    with patch("hermes_bridge.reflect.run_claude_oneshot", side_effect=fake_oneshot):
        r.maybe_promote_day_lesson()

    assert captured_users, "expected model to be called"
    user_text = captured_users[0]
    # The winning theme must be trend_day (4 occurrences > 3 for trend_day_pullback)
    assert "RECURRING THEME: trend_day\n" in user_text, (
        f"expected trend_day to win; prompt starts: {user_text[:120]}"
    )
    # The reviews sent to the model must not contain a trend_day_pullback entry —
    # exact-match filter must exclude those reviews from the trend_day prompt.
    assert "trend_day_pullback" not in user_text, (
        "trend_day_pullback reviews leaked into trend_day promotion prompt"
    )


def test_sanitize_review_text_strips_leaked_tags():
    from hermes_bridge.reflect import _sanitize_review_text
    dirty = ('Net: sitting out was correct.</narrative>\n'
             '<parameter name="observation">On confirmed trend-up days, chasing loses.')
    clean = _sanitize_review_text(dirty)
    assert "</narrative>" not in clean
    assert "<parameter" not in clean
    assert "sitting out was correct." in clean
    assert "On confirmed trend-up days, chasing loses." in clean


def test_sanitize_review_text_preserves_normal_prose():
    from hermes_bridge.reflect import _sanitize_review_text
    s = "Price closed < 30100 and delta > -0.05; the 2m close confirmed."
    assert _sanitize_review_text(s) == s


def test_reflect_on_day_drops_malformed_theme(tmp_path, monkeypatch, cfg):
    import hermes_bridge.reflect as reflect_mod

    learned = LearnedStore(str(tmp_path / "learned"))
    journal = JournalStore(str(tmp_path / "journal.jsonl"))
    r = reflect_mod.Reflector(cfg, learned, journal)

    monkeypatch.setattr(reflect_mod, "run_claude_oneshot", lambda *a, **k: "stubbed")
    monkeypatch.setattr(reflect_mod, "extract_structured", lambda reply: {
        "narrative": "A clean day.</narrative>",
        "theme": "Bad Theme With Spaces!",
        "observation": "obs<parameter name=\"x\">tail",
    })
    out = r.reflect_on_day({"date": "2026-07-01"})
    assert out["written"] == 1
    assert out["theme"] is None  # malformed theme dropped
    body = (tmp_path / "learned" / "day-reviews.md").read_text(encoding="utf-8")
    assert "</narrative>" not in body and "<parameter" not in body
    assert "obstail" in body  # observation kept, tag removed


def test_multiline_observation_flattens_to_single_footer_line(tmp_path, monkeypatch, cfg):
    import hermes_bridge.reflect as reflect_mod

    learned = LearnedStore(str(tmp_path / "learned"))
    journal = JournalStore(str(tmp_path / "journal.jsonl"))
    r = reflect_mod.Reflector(cfg, learned, journal)

    monkeypatch.setattr(reflect_mod, "run_claude_oneshot", lambda *a, **k: "stubbed")
    monkeypatch.setattr(reflect_mod, "extract_structured", lambda reply: {
        "narrative": "A clean day.",
        "theme": "clean_theme",
        "observation": "first line\nsecond line\n## not a heading",
    })
    out = r.reflect_on_day({"date": "2026-07-02"})
    assert out["written"] == 1
    revs = LearnedStore(str(tmp_path / "learned")).day_reviews(10)
    assert len(revs) == 1  # the embedded "## " did NOT split a fake section
    body_lines = [ln for ln in revs[0][1].splitlines() if ln.strip()]
    footer = body_lines[-1]
    assert footer.startswith("_theme: clean_theme")
    assert "first line second line ## not a heading" in footer  # flattened to one line
