# Playbook (Ranging) — Range Fade

**Regime fit**: ranging markets ONLY (market-regime.md). The core mean-reversion
trade: sell the top of the box, buy the bottom, target the rotation back.

## The idea

In a confirmed range, moves into the edges run out of willing aggressors and rotate
back toward the middle. You fade the edge **only after the edge proves it is holding**
— rejection plus failing flow — never just because price is "high" or "low". Edges
are where you fade; the middle is where you do nothing.

## Entry conditions (fade at range high → short; mirror at range low → long)

ALL must be true:

1. **Regime**: confirmed range — both edges respected ≥ 2 times each, EMAs flat and
   braided, box width ≥ ~3 × ATR (a narrower box can't pay for its stop).
2. **Location**: price is AT the edge — within ~0.5 × ATR of the box high
   (`swing_high` / the clustered prior highs). Middle-of-range entries are forbidden.
3. **Rejection trigger** (price-action.md): a probe to/just beyond the edge that
   **closes back inside** the box with a bearish body — a wick beyond, body back in.
4. **Flow confirms the failure** (order-flow.md): absorption (buying delta into the
   edge with no price progress) or exhaustion/divergence (new probe high on weaker
   delta). Fading a clean, accelerating breakout delta is the cardinal sin — veto.
5. **Room**: ≥ ~1R from entry back toward the middle of the box.

## Bracket

- **Stop**: just beyond the probe's extreme (≈ 1.0–1.5 × ATR total). If the edge
  truly breaks with acceptance, you want out immediately — the stop sits where the
  range thesis dies.
- **Target**: the middle of the box as the conservative default; the far edge only
  when the box is wide enough that even the midpoint pays ≥ 1R.

## Management

- Rotations are quicker than trends — take the target; don't hope a fade becomes a
  reversal trend.
- **Invalidation exit**: a bar *closes* outside the box beyond your entry edge
  (acceptance) — the range is breaking; exit without waiting for the full stop.
- Never widen the stop; never re-fade immediately after a stop-out on the same edge
  (twice-pressed edges break — see failed-breakout.md for the only re-engagement).

## Skip it when

- The box is younger than 2 touches per edge (might be a flag in a trend — fading a
  flag is fighting the trend).
- The box is narrower than ~3 × ATR.
- Price is pressing the edge repeatedly with persistent one-way delta (breakout
  pressure building).
- A scheduled-news / volatility shock just hit — ranges die on news.
