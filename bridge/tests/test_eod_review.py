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
