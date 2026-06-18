"""Calibration report — the frequency questions, answered from accumulated evidence.

Reads the live journal (closed trades) and the live decline log (counterfactuals),
plus an optional backfill corpus, and prints:
  * confidence-vs-outcome buckets (is min_confidence set right?)
  * clearance-band outcomes (is the 1xATR structural rule too strict/loose?)
  * outcome mix by counterfactual kind (live declined entries)
  * missed-trigger fill evidence (touch rate and quality of live missed_trigger records)
  * regime vs outcome split

Read-only. Run any time:
  python bridge/scripts/calibrate.py [--journal bridge/state/journal.jsonl]
      [--declines bridge/state/declines.jsonl] [--backtest bridge/state/declines_backtest.jsonl]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

CONF_BANDS = [(0.0, 0.55), (0.55, 0.7), (0.7, 0.85), (0.85, 1.01)]
CLEAR_BANDS = [(-99, 0), (0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 99)]


def load_jsonl(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def trade_clearance_atr(t: dict) -> float | None:
    """Recompute room-to-structure at entry from the journaled context."""
    c = t.get("entry_context") or {}
    atr, entry = c.get("atr"), t.get("entry_price")
    if not atr or entry is None:
        return None
    if t.get("side") == "LONG" and c.get("swing_high") is not None:
        return round((c["swing_high"] - entry) / atr, 3)
    if t.get("side") == "SHORT" and c.get("swing_low") is not None:
        return round((entry - c["swing_low"]) / atr, 3)
    return None


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", default="bridge/state/journal.jsonl")
    ap.add_argument("--declines", default="bridge/state/declines.jsonl")
    ap.add_argument("--backtest", default="bridge/state/declines_backtest.jsonl")
    args = ap.parse_args()

    trades = load_jsonl(args.journal)
    declines = load_jsonl(args.declines)
    backtest = load_jsonl(args.backtest)
    print(f"evidence: {len(trades)} closed trades, {len(declines)} live "
          f"counterfactuals, {len(backtest)} backfill counterfactuals")

    section("confidence vs outcome (live trades)")
    for lo, hi in CONF_BANDS:
        ts = [t for t in trades if lo <= float(t.get("confidence", 0)) < hi]
        if not ts:
            continue
        wins = sum(1 for t in ts if t.get("realized_pnl", 0) > 0)
        pnl = sum(t.get("realized_pnl", 0) for t in ts)
        print(f"  conf {lo:.2f}-{hi:.2f}: n={len(ts):>3} win={wins / len(ts):>4.0%} "
              f"totalPnL={pnl:+9.2f} avg={pnl / len(ts):+8.2f}")

    # Live declines no longer carry clearance_atr (missed_trigger records lack it);
    # this band is therefore journal trades + backfill corpus only.
    section("clearance band vs outcome (live trades + backfill counterfactuals)")
    rows: list[tuple[float, str]] = []
    for t in trades:
        cl = trade_clearance_atr(t)
        if cl is not None:
            rows.append((cl, "win" if t.get("realized_pnl", 0) > 0 else "loss"))
    for r in declines + backtest:
        cl, oc = r.get("clearance_atr"), r.get("outcome")
        if cl is not None and oc in ("would_win", "would_lose"):
            rows.append((cl, "win" if oc == "would_win" else "loss"))
    for lo, hi in CLEAR_BANDS:
        band = [oc for cl, oc in rows if lo <= cl < hi]
        if band:
            wins = band.count("win")
            print(f"  {lo:>4.1f}-{hi:<4.1f} xATR: n={len(band):>4} "
                  f"win={wins / len(band):>4.0%}")

    section("counterfactual outcomes by kind (live)")
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for r in declines:
        by_kind[r.get("kind", "?")][r.get("outcome", "?")] += 1
    for kind, counts in sorted(by_kind.items()):
        print(f"  {kind:<18} {dict(counts)}")

    section("missed-trigger fill evidence (live)")
    missed = [r for r in declines if r.get("kind") == "missed_trigger"]
    if not missed:
        print("  no missed_trigger records yet")
    else:
        never_filled = [r for r in missed if r.get("outcome") == "never_filled"]
        resolved = [r for r in missed if r.get("outcome") != "never_filled"]
        touch_rate = len(resolved) / len(missed)
        print(f"  live missed_trigger records: {len(missed)}  "
              f"touch rate: {len(resolved)}/{len(missed)} ({touch_rate:.0%})")
        if resolved:
            wins = sum(1 for r in resolved if r.get("outcome") == "would_win")
            print(f"  of touched: would_win {wins}/{len(resolved)} "
                  f"({wins / len(resolved):.0%})  "
                  f"never_filled: {len(never_filled)}")
            oc_counts = Counter(r.get("outcome") for r in resolved)
            print(f"  outcome breakdown: {dict(oc_counts)}")

    section("regime vs outcome (live + backfill counterfactuals)")
    by_regime: dict[str, Counter] = defaultdict(Counter)
    for r in declines + backtest:
        if r.get("regime") and r.get("outcome") in ("would_win", "would_lose"):
            by_regime[r["regime"]][r["outcome"]] += 1
    for regime, counts in sorted(by_regime.items()):
        n = sum(counts.values())
        print(f"  {regime:<9} n={n:>4} win={counts['would_win'] / n:>4.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
