"""Re-score closed trades against proposed entry gates to derive transitional_delta_floor.

First-order counterfactual (mirrors the delta-floor analysis): for each closed trade in the
journal, apply min_confidence -> transitional gate -> global delta_floor (the engine's order)
using the REAL gate functions, and sum the realized P&L of the trades that still fire. Sweeps
the transitional floor so the threshold can be set just below the cleanest transitional winner.

Run: bridge/.venv/Scripts/python.exe bridge/scripts/rescore_gates.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import et_date_session  # bridge's own DST-aware, tzdata-free ET
from hermes_bridge.models import Action, Decision

JOURNAL = Path("bridge/state/journal.jsonl")
MIN_CONF = 0.50          # proposed
DELTA_FLOOR = 0.05       # global, unchanged
GRID = [0.0, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12]


def _passes(side: str, conf: float, regime: str, dr: float, tfloor: float) -> bool:
    """Apply the proposed gates in the engine's order; True if the entry still fires."""
    act = Action.ENTER_LONG if side == "LONG" else Action.ENTER_SHORT
    d = Decision(action=act, confidence=conf, rationale="")
    if d.confidence < MIN_CONF:
        return False
    d = TradingEngine._suppress_transitional(d, regime, False, dr, tfloor)
    if d.action == Action.WAIT:
        return False
    d = TradingEngine._suppress_low_delta(d, dr, DELTA_FLOOR)
    return d.action != Action.WAIT


def main() -> None:
    rows = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        ctx = t.get("entry_context", {})
        date, session = et_date_session(t["entry_ts"])
        rows.append({
            "bucket": f"{date} {session}",
            "side": t["side"],
            "conf": float(t.get("confidence", 0.0)),
            "regime": ctx.get("regime", "?"),
            "dr": float(ctx.get("delta_ratio", 0.0)),
            "pnl": float(t["realized_pnl"]),
        })

    print(f"{len(rows)} closed trades in {JOURNAL}\n")
    print("Transitional-regime entries (the only ones this gate touches):")
    print(f"  {'bucket':<16} {'side':<5} {'conf':>5} {'delta':>7} {'pnl':>8}")
    any_trans = False
    for r in rows:
        if r["regime"] == "transitional":
            any_trans = True
            print(f"  {r['bucket']:<16} {r['side']:<5} "
                  f"{r['conf']:>5.2f} {r['dr']:>+7.3f} {r['pnl']:>8.2f}")
    if not any_trans:
        print("  (none)")

    print("\nSweep of transitional_delta_floor (net P&L of entries that still fire, per bucket):")
    for tfloor in GRID:
        per_bucket = defaultdict(float)
        kept = 0
        for r in rows:
            if _passes(r["side"], r["conf"], r["regime"], r["dr"], tfloor):
                per_bucket[r["bucket"]] += r["pnl"]
                kept += 1
        total = sum(per_bucket.values())
        buckets = "  ".join(f"{b}:{v:+.0f}" for b, v in sorted(per_bucket.items()))
        print(f"  floor={tfloor:<4} kept={kept:>2}/{len(rows)}  total={total:>+8.2f}")
        print(f"            [{buckets}]")


if __name__ == "__main__":
    main()
