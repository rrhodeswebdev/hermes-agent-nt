from hermes_bridge.config import LearningConfig


def test_eod_config_defaults_are_neutral():
    lc = LearningConfig()
    assert lc.eod_review_enabled is False
    assert lc.eod_review_cutoff_et == "16:05"
    assert lc.day_review_keep == 10
    assert lc.day_lesson_repeat_n == 3
    assert lc.day_lesson_lookback_m == 5


from hermes_bridge.memory import LearnedStore


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


import time
from hermes_bridge.reflect import build_day_digest


def _rth_ts(now):
    # a timestamp inside today's RTH window: use 14:00 ET-ish by anchoring on now.
    return now - 3600


def test_build_day_digest_counts_and_window():
    now = 1_782_750_000.0
    inwin = _rth_ts(now)
    declines = [
        {"resolved_ts": inwin, "outcome": "would_lose", "suppressed_by": "min_confidence",
         "regime": "trending", "side": "LONG", "confidence": 0.3, "delta_ratio": 0.02,
         "rationale": "pullback buy"},
        {"resolved_ts": inwin, "outcome": "would_win", "suppressed_by": "", "regime": "trending",
         "side": "LONG", "confidence": 0.18, "delta_ratio": 0.01, "rationale": "shelf hold"},
        {"resolved_ts": now - 40 * 3600, "outcome": "would_lose", "suppressed_by": "",
         "regime": "ranging", "side": "SHORT", "confidence": 0.6, "delta_ratio": -0.1,
         "rationale": "old"},
    ]
    pa = {"open": 29300.0, "high": 30060.0, "low": 29299.0, "close": 30000.0,
          "range": 761.0, "bars": 390}
    d = build_day_digest(now, declines, pa, trades=[], session={"realized_pnl": -30.0})
    assert d["declines"]["total"] == 2                       # the 40h-old one is out of window
    assert d["declines"]["by_outcome"] == {"would_lose": 1, "would_win": 1}
    assert d["declines"]["by_suppressed"]["min_confidence"] == 1
    assert d["pa"]["range"] == 761.0
    assert d["trades"]["count"] == 0


def test_build_day_digest_caps_items():
    now = 1_782_750_000.0
    declines = [{"resolved_ts": now - 3600, "outcome": "would_lose", "suppressed_by": "",
                 "regime": "trending", "side": "LONG", "confidence": 0.3, "delta_ratio": 0.0,
                 "rationale": "x"} for _ in range(40)]
    d = build_day_digest(now, declines, {"range": 0.0}, [], {})
    assert d["declines"]["total"] == 40 and len(d["declines"]["items"]) == 20
