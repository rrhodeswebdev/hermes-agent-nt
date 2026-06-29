from hermes_bridge.config import LearningConfig


def test_eod_config_defaults_are_neutral():
    lc = LearningConfig()
    assert lc.eod_review_enabled is False
    assert lc.eod_review_cutoff_et == "16:05"
    assert lc.day_review_keep == 10
    assert lc.day_lesson_repeat_n == 3
    assert lc.day_lesson_lookback_m == 5
