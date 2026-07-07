"""Shadow breakeven-tuning evaluator — pure, no I/O.

Measures what a TIGHTER, regime-gated breakeven arming would have done to a closed trade,
versus the live +1R winner-manager (``stops.managed_stop_price`` / ``engine._managed_exit``).
It NEVER touches an order or the live manager — it re-derives the same breakeven arm + close
breach so the live order path stays the single authority (see the 2026-07-06 spec).

The give-back problem: transitional-tape losers go green ~0.5R, round-trip, and stop out
before ever reaching the +1R that would pull the live stop to breakeven. This scores a
candidate ``breakeven_r_transitional`` (e.g. 0.5R) so the decision to make the live manager
regime-aware is made on accumulated evidence, not one session.

Faithfulness to production:
* arm when running MFE ≥ candidate_r × 1R (same formula as ``managed_stop_price``);
* once armed, exit at breakeven when a bar **closes** back through entry (same close-breach
  test as ``engine._managed_exit`` — NOT an intrabar touch);
* a breakeven exit realizes 0 gross (exit at entry); never armed / never close-breached →
  the trade keeps its actual outcome.
Gated to ``regime == "transitional"`` — the sim says never manage a trending runner.
"""

from __future__ import annotations

from .config import BridgeConfig
from .journal import ClosedTrade
from .models import Bar


def shadow_breakeven_outcome(
    trade: ClosedTrade, bars: list[Bar], cfg: BridgeConfig
) -> dict | None:
    """Score a candidate transitional breakeven arming for one closed trade.

    Returns a ``kind="shadow_breakeven"`` record, or ``None`` when the shadow does not
    apply: feature off (``shadow_breakeven_r_transitional`` ≤ 0), non-transitional regime,
    or no usable stop (can't derive 1R). ``bars`` are the trade's decision bars in
    ``[entry_ts, exit_ts]`` (order preserved); an empty list yields a ``no_change`` record.
    """
    candidate_r = cfg.strategy.shadow_breakeven_r_transitional
    if candidate_r <= 0:
        return None
    regime = str((trade.entry_context or {}).get("regime", ""))
    if regime != "transitional":
        return None
    if not trade.stop_price:
        return None  # 0.0 = unattributed stop (journal sentinel) → can't express +R arming
    entry = float(trade.entry_price)
    one_r = abs(entry - float(trade.stop_price))
    if one_r <= 0:
        return None

    long = trade.side == "LONG"
    arm_at = candidate_r * one_r
    mfe = 0.0
    armed = False
    breakeven_exit = False
    for b in bars:
        fav = (b.high - entry) if long else (entry - b.low)
        if fav > mfe:
            mfe = fav
        if not armed and mfe >= arm_at:
            armed = True
        if armed:
            breached = (b.close <= entry) if long else (b.close >= entry)
            if breached:
                breakeven_exit = True
                break

    actual = float(trade.realized_pnl)
    if breakeven_exit:
        shadow_pnl = 0.0  # exit at entry = breakeven, gross
        if actual < 0:
            outcome = "saved_loser"
        elif actual > 0:
            outcome = "scratched_winner"
        else:
            outcome = "no_change"
    else:
        shadow_pnl = actual  # never armed, or armed but no close-breach → unchanged
        outcome = "no_change"

    return {
        "kind": "shadow_breakeven",
        "regime": regime,
        "entry_ts": trade.entry_ts,
        "resolved_ts": trade.exit_ts,
        "candidate_r": candidate_r,
        "actual_pnl": round(actual, 2),
        "shadow_pnl": round(shadow_pnl, 2),
        "delta": round(shadow_pnl - actual, 2),
        "outcome": outcome,
        "mfe": round(float(trade.mfe), 4),
        "one_r_pts": round(one_r, 4),
        "armed": armed,
    }
