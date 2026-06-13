# Playbook (Trending) — Trend Pullback

**Regime fit**: trending markets ONLY (market-regime.md). The bread-and-butter
with-trend entry: join an established trend at value after a counter-trend pause.

## The idea

Trends move in legs: impulse → pullback → impulse. You buy the dip to *value* in an
uptrend (sell the bounce in a downtrend) exactly as the trend resumes — buying
strength on sale, not predicting reversals.

## Entry conditions (long; mirror for short)

ALL must be true:

1. **Regime**: trending up — advancing higher highs/higher lows, fast EMA above slow
   with real separation, price respecting the fast EMA.
2. **Pullback to value**: the bar's low tags or undercuts `ema_fast` (within ~0.5 ×
   ATR) — a genuine dip, not a bar floating far above value. A pullback into a prior
   swing or broken level that coincides with the EMA is the highest-quality location.
3. **Held structure**: the pullback stays above the prior higher-low (`swing_low`). A
   pullback that breaks structure is a reversal candidate, not this setup.
4. **Resumption trigger**: the bar **closes back above** `ema_fast` with a decisive
   bullish body (close > open).
5. **Flow confirms** (order-flow.md): `recent_delta` neutral-to-positive, ideally
   turning up on the trigger. Clearly negative delta on the trigger = veto.
6. **Room**: ≥ ~1R to the next opposing structure (`swing_high`). Entering directly
   into the prior high with no room is a veto.

## Bracket

- **Stop**: ~1.5 × ATR from entry, and beyond the pullback's low (whichever is the
  trade's true invalidation). The stop lives where the *setup is wrong*.
- **Target**: ~2 × ATR (≈ 1.3R), or just in front of the next structure if closer —
  never place a target beyond the first wall.

## Management

- The resting bracket handles normal stop-out and target. **Let it work.**
- Exit early ONLY on invalidation: trend flips (structure break / EMAs cross against
  you) or delta turns clearly against the position while price loses the slow EMA.
- Never widen the stop. Early exit allowed; added risk never.

## Skip it when

- The trend is mature/extended (many legs old, far from any base) — late-trend
  pullbacks fail more (see regime "age", market-regime.md).
- The pullback is climactic (huge counter-trend bars) — that's a reversal attempt.
- The dip never reached value (chasing) or broke the prior swing (structure gone).
- Delta is firmly against the trigger.
