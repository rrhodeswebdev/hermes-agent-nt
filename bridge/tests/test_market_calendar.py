"""US equity-futures market calendar: full holidays (stand down) and early-close half days
(13:00 ET — flatten before the close). Without it hermes treats every weekday as a normal
09:30-16:00 session and would carry a position into the holiday/weekend gap (the Juneteenth
2026 case the operator had to flatten by hand)."""

from datetime import UTC, datetime

from hermes_bridge.market_calendar import (
    closing_reason,
    early_close_minute,
    holiday_name,
    within_close_cutoff,
)


def _ts(y: int, mo: int, d: int, h: int, mi: int) -> float:
    """A UTC timestamp. ET = UTC-4 (EDT, ~Mar-Nov) or UTC-5 (EST); callers pass the UTC
    wall time that maps to the ET time they mean (e.g. 16:50 UTC = 12:50 EDT in June)."""
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


# --- full holidays (computed, no annual table) ----------------------------- #
def test_juneteenth_2026_is_a_holiday():
    # 2026-06-19 is a Friday; 14:00 UTC = 10:00 EDT.
    assert holiday_name(_ts(2026, 6, 19, 14, 0)) == "Juneteenth"


def test_thanksgiving_2026_is_a_holiday():
    # 4th Thursday of Nov 2026 = Nov 26; 16:00 UTC = 11:00 EST.
    assert holiday_name(_ts(2026, 11, 26, 16, 0)) == "Thanksgiving"


def test_good_friday_2026_is_a_holiday():
    # Easter 2026 = Apr 5; Good Friday = Apr 3.
    assert holiday_name(_ts(2026, 4, 3, 14, 0)) == "Good Friday"


def test_normal_weekday_is_not_a_holiday():
    # 2026-06-18 Thursday — an ordinary session.
    assert holiday_name(_ts(2026, 6, 18, 14, 0)) is None
    assert early_close_minute(_ts(2026, 6, 18, 14, 0)) is None


# --- early-close half days (13:00 ET = minute 780) ------------------------- #
def test_black_friday_2026_is_early_close():
    # Day after Thanksgiving = Nov 27 2026.
    assert early_close_minute(_ts(2026, 11, 27, 16, 0)) == 780
    assert holiday_name(_ts(2026, 11, 27, 16, 0)) is None


def test_christmas_eve_2026_is_early_close():
    # Dec 24 2026 is a Thursday (Dec 25 Fri = Christmas).
    assert early_close_minute(_ts(2026, 12, 24, 16, 0)) == 780


# --- within_close_cutoff drives the engine flatten / entry cutoff ----------- #
def test_holiday_is_within_cutoff_all_day():
    # A full holiday: stand down regardless of time of day.
    assert within_close_cutoff(_ts(2026, 6, 19, 14, 0), 15) is True


def test_early_close_cutoff_fires_inside_the_lead():
    # Black Friday 12:50 EST = 17:50 UTC; 13:00 close, 15-min lead → cutoff at 12:45.
    assert within_close_cutoff(_ts(2026, 11, 27, 17, 50), 15) is True


def test_early_close_morning_is_not_within_cutoff():
    # Black Friday 11:00 EST = 16:00 UTC — still tradeable.
    assert within_close_cutoff(_ts(2026, 11, 27, 16, 0), 15) is False


def test_normal_day_is_never_within_cutoff():
    assert within_close_cutoff(_ts(2026, 6, 18, 19, 0), 15) is False


def test_lead_zero_disables_the_cutoff():
    # <= 0 lead is the off-switch — even a holiday is not gated by the engine then.
    assert within_close_cutoff(_ts(2026, 6, 19, 14, 0), 0) is False


def test_closing_reason_names_the_cause():
    assert closing_reason(_ts(2026, 6, 19, 14, 0)) == "holiday:Juneteenth"
    assert closing_reason(_ts(2026, 11, 27, 16, 0)) == "early_close"
    assert closing_reason(_ts(2026, 6, 18, 14, 0)) is None
