"""Counterfactual backfill — replay stored bars through the deterministic mock
prefilter (structural-regime gating) and score every candidate with the
strategy's ATR brackets.

Generates the statistics the live decline stream accumulates at ~15/day, at
thousands-per-month scale: clearance-band win rates and regime splits.

The output is a SEPARATE corpus from the live journal/declines (tagged
source=backfill) — reflection never reads it. Statistics come from history;
judgment keeps coming from trades the agent actually lived through.

Usage:
  python bridge/scripts/backfill_counterfactuals.py --db bridge/state/bars.db \\
      [--config config/trading.yaml] [--out bridge/state/declines_backtest.jsonl] \\
      [--horizon 30] [--dedup-bars 5]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402 — bridge/

from hermes_bridge.agent_client import AgentRequest, MockAgentClient  # noqa: E402
from hermes_bridge.config import load_config  # noqa: E402
from hermes_bridge.indicators import build_context  # noqa: E402
from hermes_bridge.models import Action, Bar  # noqa: E402
from hermes_bridge.session import SessionState  # noqa: E402

_WINDOW = 200


def load_bars(db_path: str, instrument: str | None, timeframe: str | None) -> list[Bar]:
    db = sqlite3.connect(db_path)
    where, args = "", []
    if instrument:
        where, args = " WHERE instrument=?", [instrument]
        if timeframe:
            where += " AND timeframe=?"
            args.append(timeframe)
    rows = db.execute(
        f"SELECT ts, open, high, low, close, volume, ask_volume, bid_volume "
        f"FROM bars{where} ORDER BY ts",
        args,
    ).fetchall()
    return [
        Bar(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5],
            ask_volume=r[6], bid_volume=r[7])
        for r in rows
    ]


def resolve(
    side: str, entry: float, stop: float, target: float, forward: list[Bar]
) -> tuple[str, int]:
    """First-touch bracket resolution (same conservative rules as the engine)."""
    is_long = side == "LONG"
    for i, b in enumerate(forward, start=1):
        hit_t = b.high >= target if is_long else b.low <= target
        hit_s = b.low <= stop if is_long else b.high >= stop
        if hit_t and hit_s:
            return "ambiguous", i
        if hit_t:
            return "would_win", i
        if hit_s:
            return "would_lose", i
    return "no_resolution", len(forward)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="bridge/state/bars.db")
    ap.add_argument("--config", default="config/trading.yaml")
    ap.add_argument("--out", default="bridge/state/declines_backtest.jsonl")
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--dedup-bars", type=int, default=5)
    ap.add_argument("--instrument", default=None)
    ap.add_argument("--timeframe", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    bars = load_bars(
        args.db,
        args.instrument or cfg.instrument.symbol,
        args.timeframe or cfg.instrument.timeframe,
    )
    if len(bars) < _WINDOW + args.horizon:
        print(f"not enough bars ({len(bars)}) — need {_WINDOW + args.horizon}+")
        return 1
    print(f"replaying {len(bars)} bars "
          f"({cfg.instrument.symbol} {cfg.instrument.timeframe})...")

    mock = MockAgentClient(cfg)
    session = SessionState(
        cfg.instrument.symbol, cfg.instrument.timeframe,
        cfg.instrument.tick_size, cfg.instrument.tick_value,
        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss,
    )
    sm, tm = cfg.strategy.atr_stop_mult, cfg.strategy.atr_target_mult

    records: list[dict] = []
    last_candidate: tuple[str, int] | None = None  # (side, index) for dedup
    for i in range(_WINDOW - 1, len(bars) - args.horizon):
        window = bars[i - _WINDOW + 1: i + 1]
        ctx = build_context(
            window,
            atr_period=cfg.strategy.atr_period,
            swing_lookback=cfg.strategy.swing_lookback,
            level_bars=bars[max(0, i - 7000): i + 1],
        )
        if ctx.atr is None or ctx.atr <= 0:
            continue
        d = mock.decide(AgentRequest(
            mode="seek_entry", context=ctx, recent_bars=window,
            account=session.account_state(mark_price=ctx.last_close),
        ))
        if d.action not in (Action.ENTER_LONG, Action.ENTER_SHORT):
            continue
        side = "LONG" if d.action == Action.ENTER_LONG else "SHORT"
        if (last_candidate and last_candidate[0] == side
                and i - last_candidate[1] < args.dedup_bars):
            continue
        last_candidate = (side, i)
        bar = bars[i]
        sign = 1.0 if side == "LONG" else -1.0
        forward = bars[i + 1: i + 1 + args.horizon]
        outcome, n = resolve(
            side, bar.close,
            bar.close - sign * sm * ctx.atr,
            bar.close + sign * tm * ctx.atr,
            forward,
        )
        room = (
            (ctx.swing_high - bar.close)
            if side == "LONG" and ctx.swing_high is not None
            else (bar.close - ctx.swing_low)
            if side == "SHORT" and ctx.swing_low is not None
            else None
        )
        records.append({
            "source": "backfill",
            "ts": bar.ts,
            "kind": "candidate",
            "side": side,
            "entry_price": round(bar.close, 4),
            "outcome": outcome,
            "bars_to_resolve": n,
            "clearance_atr": round(room / ctx.atr, 3) if room is not None else None,
            "regime": ctx.regime,
            "session": ctx.session,
            "weekday": ctx.weekday,
            "clock_et": ctx.clock_et,
            "trend": ctx.trend,
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print(f"wrote {len(records)} candidate counterfactuals -> {out}\n")

    # ---- summary ----------------------------------------------------------
    by_outcome = Counter(r["outcome"] for r in records)
    print("outcomes:", dict(by_outcome))
    bands = [(0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 99)]
    print("\nclearance-band win rates (would_win / decided):")
    for lo, hi in bands:
        rs = [
            r for r in records
            if r["clearance_atr"] is not None
            and lo <= r["clearance_atr"] < hi
            and r["outcome"] in ("would_win", "would_lose")
        ]
        if rs:
            wins = sum(1 for r in rs if r["outcome"] == "would_win")
            print(f"  {lo:>4.1f}-{hi:<4.1f} xATR: {wins}/{len(rs)} "
                  f"= {wins / len(rs):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
