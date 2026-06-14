"""Major-news blackout guard — keep the agent OUT of trades around high-impact events.

The bridge fetches an economic-calendar JSON feed (ForexFactory weekly mirror by default)
and the RiskGate consults ``blackout_at(now_ts)`` on every ENTRY. Within
``window_minutes`` of a high-impact event for a configured currency, new entries are
rejected; exits are never affected. Deterministic and server-side, like the daily-goal and
session-hours gates — never delegated to the LLM.

Fail-open by design: the network fetch runs on a background thread (never the hot path), a
failed/empty fetch keeps the last-good calendar (cache), and with nothing cached the gate
simply allows trading. ``blackout_at`` itself is pure (in-memory) so the RiskGate stays
I/O-free and unit-testable.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from .config import BridgeConfig

_USER_AGENT = "hermes-bridge/news"
# ForexFactory sits behind Cloudflare and is friendlier to a browser-like UA than a bot one.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class NewsEvent:
    title: str
    currency: str
    impact: str
    ts: float  # event time as a Unix epoch (seconds)

    def label(self) -> str:
        return f"{self.currency}:{self.impact}:{self.title}"


def _parse_events(
    raw: list, block_impacts: list[str], currencies: list[str]
) -> list[NewsEvent]:
    """Pure: turn raw feed rows into matching high-impact events. Rows that are the wrong
    impact/currency, or that don't parse, are skipped (robust against feed quirks)."""
    impacts = {s.strip().lower() for s in block_impacts}
    ccys = {s.strip().upper() for s in currencies}
    out: list[NewsEvent] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        impact = str(row.get("impact", "")).strip()
        currency = str(row.get("country", "")).strip()
        if impact.lower() not in impacts or currency.upper() not in ccys:
            continue
        try:
            dt = datetime.fromisoformat(str(row.get("date", "")).replace("Z", "+00:00"))
            ts = dt.timestamp()  # tz-aware → true epoch
        except (ValueError, TypeError):
            continue
        out.append(
            NewsEvent(title=str(row.get("title", "")).strip() or "event",
                      currency=currency, impact=impact, ts=ts)
        )
    return out


def _balanced_slice(s: str, start: int) -> int:
    """Index just past the bracket/brace at ``s[start]`` that matches it, respecting JSON
    string literals (so brackets inside quoted values don't unbalance the count). -1 if none."""
    open_ch = s[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def _parse_forexfactory(
    html: str, block_impacts: list[str], currencies: list[str]
) -> list[NewsEvent]:
    """Pure: extract matching events from a ForexFactory calendar page. The page embeds its
    data as ``window.calendarComponentStates[N] = { days:[ {..,"events":[{...}]} ] }``; each
    event object carries quoted-key JSON with ``currency``, ``impactName`` (low|medium|high),
    ``dateline`` (a Unix epoch), and ``name`` — so we pull every ``"events":[...]`` array,
    parse it as JSON, and map it. Malformed/empty arrays are skipped (robust)."""
    impacts = {s.strip().lower() for s in block_impacts}
    ccys = {s.strip().upper() for s in currencies}
    out: list[NewsEvent] = []
    seen: set = set()  # FF embeds the calendar blob more than once per page — dedupe by event id
    key = '"events":'
    idx = 0
    while True:
        k = html.find(key, idx)
        if k < 0:
            break
        b = html.find("[", k)
        if b < 0:
            break
        end = _balanced_slice(html, b)
        if end < 0:
            idx = k + len(key)
            continue
        idx = end
        try:
            events = json.loads(html[b:end])
        except (ValueError, TypeError):
            continue
        for ev in events if isinstance(events, list) else []:
            if not isinstance(ev, dict):
                continue
            impact = str(ev.get("impactName", "")).strip().lower()
            currency = str(ev.get("currency", "")).strip().upper()
            if impact not in impacts or currency not in ccys:
                continue
            try:
                ts = float(ev["dateline"])  # FF datelines are Unix epochs
            except (KeyError, TypeError, ValueError):
                continue
            title = str(ev.get("name", "")).strip() or "event"
            dedupe_key = ev.get("id", (currency, title, ts))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append(NewsEvent(title=title, currency=currency, impact=impact.title(), ts=ts))
    return out


class NewsGuard:
    """Holds the fetched calendar and answers ``blackout_at``. One instance is shared by the
    server (which drives ``refresh``) and the RiskGate (which only reads ``blackout_at``)."""

    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config.news
        self._events: list[NewsEvent] = []
        self._last_ok_ts: float | None = None      # last SUCCESSFUL fetch
        self._last_attempt_ts: float | None = None  # last attempt (ok or not)
        self._error: str | None = None

    # ---- fetch (server / background only) -----------------------------------
    def _fetch_raw(self) -> list:
        """Network GET of the JSON feed → parsed JSON list. Isolated so it can be stubbed."""
        req = urllib.request.Request(self.cfg.feed_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.cfg.fetch_timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _fetch_html(self) -> str:
        """Network GET of the ForexFactory calendar page → HTML. Isolated so it can be stubbed."""
        req = urllib.request.Request(
            self.cfg.forexfactory_url, headers={"User-Agent": _BROWSER_UA}
        )
        with urllib.request.urlopen(req, timeout=self.cfg.fetch_timeout_s) as resp:
            return resp.read().decode("utf-8", "replace")

    def refresh(self, now: float | None = None) -> bool:
        """Fetch + parse the calendar (per ``source``). On success, replace the cache; on
        failure, KEEP the last-good cache (fail-open) and record the error. Returns True on
        success."""
        if not self.cfg.enabled:
            return False
        self._last_attempt_ts = now
        try:
            if self.cfg.source == "forexfactory":
                self._events = _parse_forexfactory(
                    self._fetch_html(), self.cfg.block_impacts, self.cfg.currencies)
            else:
                self._events = _parse_events(
                    self._fetch_raw(), self.cfg.block_impacts, self.cfg.currencies)
            self._last_ok_ts = now
            self._error = None
            return True
        except Exception as exc:  # noqa: BLE001 — any failure must degrade to fail-open
            self._error = f"{type(exc).__name__}: {exc}"
            return False

    def maybe_refresh(self, now: float) -> bool:
        """Refresh if enabled and we've never fetched or the cadence has elapsed."""
        if not self.cfg.enabled:
            return False
        due = self._last_attempt_ts is None or (
            now - self._last_attempt_ts >= self.cfg.refresh_minutes * 60.0
        )
        return self.refresh(now) if due else False

    # ---- read (RiskGate / dashboard — pure) ---------------------------------
    def blackout_at(self, now_ts: float | None) -> NewsEvent | None:
        """The high-impact event whose ± ``window_minutes`` blackout covers ``now_ts``, or
        None. Disabled guard, missing time, or empty cache ⇒ None (trading proceeds)."""
        if not self.cfg.enabled or now_ts is None:
            return None
        window_s = self.cfg.window_minutes * 60.0
        nearest: NewsEvent | None = None
        for ev in self._events:
            if abs(now_ts - ev.ts) <= window_s:
                if nearest is None or abs(now_ts - ev.ts) < abs(now_ts - nearest.ts):
                    nearest = ev
        return nearest

    def status(self, now_ts: float | None = None) -> dict:
        """Snapshot for /health + the dashboard (fail-open visibility)."""
        active = self.blackout_at(now_ts)
        upcoming = (
            sorted((e for e in self._events if now_ts is None or e.ts >= now_ts),
                   key=lambda e: e.ts)
            if self._events else []
        )
        nxt = upcoming[0] if upcoming else None
        return {
            "enabled": self.cfg.enabled,
            "ok": self._error is None and self._last_ok_ts is not None,
            "error": self._error,
            "last_fetch_ts": self._last_ok_ts,
            "event_count": len(self._events),
            "blackout_active": active is not None,
            "active_event": active.label() if active else None,
            "next_event": ({"title": nxt.title, "currency": nxt.currency, "ts": nxt.ts}
                           if nxt else None),
        }
