"""Bar resampler: aggregate a fine feed-timeframe bar stream (e.g. 1m) into a coarser
decision timeframe (e.g. 2m), auto-selected by trading session. Keeps the NinjaTrader chart
on a fixed feed timeframe forever; the bridge owns the decision cadence. See
docs/superpowers/specs/2026-06-17-bridge-bar-resampler-design.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .config import InstrumentConfig, timeframe_seconds
from .indicators import session_for_ts
from .models import Bar
from .store import BarStore


def feed_tf_of(inst: InstrumentConfig) -> str:
    """The raw feed timeframe NinjaTrader streams; empty => the legacy `timeframe`."""
    return inst.feed_timeframe or inst.timeframe


def _resolved_decision_tf(inst: InstrumentConfig) -> str:
    """The decision timeframe when it is STATIC. For 'auto' this is undefined (it varies by
    session) — callers must check `decision_timeframe == 'auto'` first."""
    return inst.timeframe if inst.decision_timeframe == "static" else inst.decision_timeframe


def resampler_engaged(inst: InstrumentConfig) -> bool:
    """True when the resampler must run: 'auto' (session-driven) or a fixed decision timeframe
    that differs from the feed. Otherwise the ingest path is unchanged from today."""
    if inst.decision_timeframe == "auto":
        return True
    return feed_tf_of(inst) != _resolved_decision_tf(inst)


def _sum_opt(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def aggregate_bars(bars: list[Bar]) -> Bar:
    """Combine consecutive feed bars into one bar. ts = the last (closing) bar's ts."""
    return Bar(
        ts=bars[-1].ts,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
        bid_volume=_sum_opt([b.bid_volume for b in bars]),
        ask_volume=_sum_opt([b.ask_volume for b in bars]),
    )


def _fold(bars: list[Bar], step: int) -> tuple[list[Bar], list[Bar]]:
    """Fold a feed-bar list into (complete decision bars, trailing in-progress accumulator).
    A decision bar closes on a bar whose ts aligns to the step (ts % step == 0)."""
    complete: list[Bar] = []
    accum: list[Bar] = []
    for b in bars:
        accum.append(b)
        if int(b.ts) % step == 0:
            complete.append(aggregate_bars(accum))
            accum = []
    return complete, accum


def resample_series(bars: list[Bar], step: int) -> list[Bar]:
    """Complete decision bars only (trailing partial dropped)."""
    return _fold(bars, step)[0]


class Resampler:
    """Owns the feed->decision bar stream and the session-driven timeframe switch.

    Holds the persisted feed store (raw feed bars) and the in-memory decision store (the
    engine's store). On a switch it rebuilds the decision store losslessly from the feed store.
    """

    def __init__(
        self,
        feed_store: BarStore,
        decision_store: BarStore,
        *,
        feed_tf: str,
        decision_timeframe: str,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.feed_store = feed_store
        self.decision_store = decision_store
        self.feed_tf = feed_tf
        # None => schedule by session; else a fixed override ("1m"/"2m"/...).
        self.override: str | None = None if decision_timeframe == "auto" else decision_timeframe
        self._accum: list[Bar] = []
        self.current_tf: str = self.override or self.scheduled_tf(now_fn())

    def scheduled_tf(self, ts: float) -> str:
        """The timeframe the schedule wants for this bar's ts."""
        if self.override is not None:
            return self.override
        return "2m" if session_for_ts(ts) == "RTH" else "1m"

    def _ingest_aggregate(self, bar: Bar) -> Bar | None:
        """Fold one feed bar into the current decision bar; emit it when the bar closes the
        decision window. Passthrough (returns the bar unchanged) when decision TF == feed TF."""
        if self.current_tf == self.feed_tf:
            return bar
        self._accum.append(bar)
        step = int(timeframe_seconds(self.current_tf))
        if int(bar.ts) % step == 0:
            out = aggregate_bars(self._accum)
            self._accum = []
            return out
        return None

    def on_feed_bar(self, bar: Bar, *, is_flat: bool) -> Bar | None:
        """Ingest one raw feed bar: persist it, switch the decision TF if the schedule wants a
        change and we are flat, then return a completed decision bar (or None while forming).

        Deferral needs no stored flag: the schedule is recomputed every bar, so while not flat
        we simply do not switch; the first flat bar whose schedule still differs flips it."""
        self.feed_store.append(bar)
        desired = self.scheduled_tf(bar.ts)
        if desired != self.current_tf and is_flat:
            # Switch: rebuild the decision store from the feed (which now includes `bar`). The
            # rebuild already consumed `bar`, so surface the just-closed decision bar FROM the
            # rebuild instead of re-folding `bar` here (which would double-count it).
            self._switch_to(desired)
            if self.current_tf == self.feed_tf:
                return bar  # switched to passthrough; the feed bar is itself the decision bar
            last = self.decision_store.last()
            if last is not None and int(last.ts) == int(bar.ts):
                return last  # `bar` closed a decision window during the rebuild
            return None  # `bar` is mid-window (now buffered in self._accum)
        return self._ingest_aggregate(bar)

    def pending_switch(self, ts: float) -> str | None:
        """The timeframe a switch is waiting to apply (deferred until flat), else None."""
        desired = self.scheduled_tf(ts)
        return desired if desired != self.current_tf else None

    def _switch_to(self, tf: str) -> None:
        """Rebuild the decision store from the feed store at `tf`, carrying the trailing
        in-progress window so the switch loses no feed data."""
        self.current_tf = tf
        self.decision_store.timeframe = tf
        feed = self.feed_store.all()
        if tf == self.feed_tf:
            self.decision_store.replace_history(feed)
            self._accum = []
            return
        complete, partial = _fold(feed, int(timeframe_seconds(tf)))
        self.decision_store.replace_history(complete)
        self._accum = partial

    def initial_rebuild(self) -> None:
        """Build the decision store from whatever feed history loaded at startup."""
        self._switch_to(self.current_tf)

    def replace_feed_history(self, bars: list[Bar]) -> int:
        """Bulk-load feed history (NinjaTrader /ingest/history) and rebuild the decision store."""
        self.feed_store.replace_history(bars)
        self._switch_to(self.current_tf)
        return len(self.decision_store)
