"""US equity-index-futures market calendar: full holidays and early-close (half) days.

hermes day-trades MNQ (CME equity index futures) around the RTH session. With no calendar it
treats every weekday as a normal 09:30-16:00 session — so it would carry a position into a
holiday/weekend gap and never flatten before an exchange early close (the Juneteenth 2026 gap
the operator had to flatten by hand), and it would trade thin holiday tape.

This module is pure date logic (no I/O). For a bar timestamp it answers:
  - is the bar's ET date a full market holiday?       -> stand down all day, flatten
  - is it an early-close half day (13:00 ET)?         -> wind down + flatten before the close
Movable holidays (MLK, Good Friday, Memorial Day, Thanksgiving, ...) are COMPUTED, so there is
no annual table to maintain. ET conversion reuses indicators' DST-correct offset.

Policy — it errs toward standing DOWN: the full-holiday list flags the whole session closed
even though the futures trade a thin morning on some of them, because sitting out a thin
holiday is the safe, operator-endorsed outcome (Juneteenth 2026: "$0 / 0 trades, sat out the
thin half-day"). A wrongly-flagged session costs a missed day, never an unprotected one.
"""

from __future__ import annotations

from datetime import date, timedelta

from .indicators import _EPOCH, _eastern_offset

# CME equity-index-future early close: 13:00 ET (12:00 CT). Minutes since ET midnight.
EARLY_CLOSE_MINUTE = 13 * 60


def _et(ts: float):
    """The bar's wall-clock datetime in US Eastern (DST-correct; Windows-safe epoch math)."""
    dt_utc = _EPOCH + timedelta(seconds=ts)
    return dt_utc + _eastern_offset(dt_utc)


def _et_date(ts: float) -> date:
    et = _et(ts)
    return date(et.year, et.month, et.day)


def _et_minute(ts: float) -> int:
    et = _et(ts)
    return et.hour * 60 + et.minute


# ---- movable-date helpers ------------------------------------------------- #
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0 .. Sun=6) of a month, e.g. 3rd Monday = MLK Day."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` of a month, e.g. last Monday of May = Memorial Day."""
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm) — Good Friday is two days before."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE-style weekend observance: a Saturday holiday is taken Friday, a Sunday Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _full_holidays(year: int) -> dict[date, str]:
    """Dates hermes stands fully down (observed). Cached per year via lru-free recompute —
    cheap (a handful of date ops) and called at most once per bar."""
    return {
        _observed(date(year, 1, 1)): "New Year's Day",
        _nth_weekday(year, 1, 0, 3): "MLK Day",
        _nth_weekday(year, 2, 0, 3): "Presidents Day",
        _easter(year) - timedelta(days=2): "Good Friday",
        _last_weekday(year, 5, 0): "Memorial Day",
        _observed(date(year, 6, 19)): "Juneteenth",
        _observed(date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving",  # 4th Thursday (Thu=3)
        _observed(date(year, 12, 25)): "Christmas",
    }


def _early_close_days(year: int) -> dict[date, str]:
    """Half days: futures close 13:00 ET, then a holiday/weekend gap follows. Excludes any
    date that is itself an observed full holiday (e.g. July 3 when July 4 falls on Saturday)."""
    out: dict[date, str] = {}
    full = set(_full_holidays(year))
    thanks = _nth_weekday(year, 11, 3, 4)
    candidates = {
        thanks + timedelta(days=1): "Black Friday",      # always a weekday
        date(year, 12, 24): "Christmas Eve",
        date(year, 7, 3): "Independence Day eve",
    }
    for d, name in candidates.items():
        if d.weekday() < 5 and d not in full:
            out[d] = name
    return out


# ---- public API ----------------------------------------------------------- #
def holiday_name(ts: float) -> str | None:
    """The full-holiday name for the bar's ET date, or None."""
    d = _et_date(ts)
    return _full_holidays(d.year).get(d)


def early_close_minute(ts: float) -> int | None:
    """13:00 ET (minute 780) if the bar's ET date is a futures early-close half day, else None."""
    d = _et_date(ts)
    return EARLY_CLOSE_MINUTE if d in _early_close_days(d.year) else None


def closing_reason(ts: float) -> str | None:
    """Why this session is (or is about to be) closed: ``holiday:<name>`` on a full holiday,
    ``early_close`` on a half day, else None. Used as the flatten command's reason / the
    seek-entry WAIT rationale."""
    h = holiday_name(ts)
    if h is not None:
        return f"holiday:{h}"
    if early_close_minute(ts) is not None:
        return "early_close"
    return None


def within_close_cutoff(ts: float, lead_min: int) -> bool:
    """Should the engine flatten any open position and take no new entries on this bar?

    True on a full holiday (all day), or on an early-close day once within ``lead_min`` minutes
    of the 13:00 ET close. ``lead_min <= 0`` disables the feature entirely (the off-switch)."""
    if lead_min <= 0:
        return False
    if holiday_name(ts) is not None:
        return True
    ec = early_close_minute(ts)
    return ec is not None and _et_minute(ts) >= ec - lead_min
