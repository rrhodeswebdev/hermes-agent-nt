"""Repair journal timestamps written with a timezone-skewed bar clock.

Until 2026-06-11 the NinjaTrader strategy stamped bar timestamps via
`DateTime.ToUniversalTime()` on chart-display-time (ET) interpreted as machine
time (PT) — every ts landed +3h ahead of true UTC, so `session` labels computed
from them misclassify (e.g. RTH trades after 13:00 ET logged as ETH).

This shifts entry_ts / exit_ts / entry_context.ts by --offset-hours and
recomputes entry_context.session from the corrected entry_ts. Dry-run by
default; --apply writes (after backing up the journal next to itself).

Usage:
    python scripts/repair_journal_ts.py --journal state/journal.jsonl \
        --offset-hours -3 [--apply]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_bridge.indicators import session_for_ts  # noqa: E402


def _et_str(ts: float) -> str:
    # Render close enough for eyeballing (UTC; the point is the before/after delta).
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%m-%d %H:%M UTC")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--offset-hours", type=float, required=True,
                    help="hours to ADD to every stored ts (skew was +3h -> pass -3)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    path = Path(args.journal)
    if not path.is_file():
        print(f"no journal at {path}")
        return 1
    offset = args.offset_hours * 3600.0

    out_lines: list[str] = []
    changed = 0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        t = json.loads(line)
        old_entry_ts = t.get("entry_ts")
        ec = t.get("entry_context") or {}
        old_session = ec.get("session")

        for key in ("entry_ts", "exit_ts"):
            if isinstance(t.get(key), (int, float)):
                t[key] = t[key] + offset
        if isinstance(ec.get("ts"), (int, float)):
            ec["ts"] = ec["ts"] + offset
        new_session = session_for_ts(t["entry_ts"]) if t.get("entry_ts") else old_session
        if isinstance(ec, dict) and new_session is not None:
            # Derived from the corrected ts — also backfills pre-session-field entries.
            ec["session"] = new_session

        mark = " *SESSION CHANGED*" if new_session != old_session else ""
        print(f"#{i}: {_et_str(old_entry_ts)} -> {_et_str(t['entry_ts'])}  "
              f"session {old_session} -> {new_session}{mark}  "
              f"({t.get('side')} pnl={t.get('realized_pnl')})")
        if new_session != old_session or offset != 0:
            changed += 1
        out_lines.append(json.dumps(t, separators=(",", ":")))

    if not args.apply:
        print(f"\nDRY RUN — {changed} entries would change. Re-run with --apply.")
        return 0

    backup = path.with_suffix(f".jsonl.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"\nAPPLIED — {changed} entries rewritten. Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
