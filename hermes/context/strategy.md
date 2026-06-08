# Strategy — Trend Pullback with Order-Flow Confirmation

This is the **only** setup you trade. If the current bar is not this setup, you WAIT.

## The idea

Trade **with** the established trend, entering on a **pullback** that **tags a moving
average and then resumes**, confirmed by **order flow**. You are buying strength on a
dip (or selling weakness on a bounce), not predicting reversals.

## Definitions (provided to you each bar in `context`)

- `trend` — `up` when fast EMA > slow EMA, `down` when fast EMA < slow EMA.
- `ema_fast`, `ema_slow` — the moving averages (defaults 9 / 21).
- `atr` — Average True Range; your unit of "normal" movement.
- `recent_delta` — cumulative order-flow delta over the recent window (see order-flow.md).
- `swing_high`, `swing_low` — the last confirmed pivots (structure).

## Long setup (mirror for shorts)

Take an **ENTER_LONG** only when ALL are true:

1. **Trend is up** (`trend == "up"`).
2. **Pullback tagged the fast EMA**: the bar's low dipped to/through `ema_fast`
   (within ~0.5 × ATR), i.e. a real dip, not an extended bar far above the EMA.
3. **Resumption**: the bar **closed back above** `ema_fast` and is a **bullish bar**
   (close > open). This is the "and then resumes" trigger.
4. **Order flow confirms**: `recent_delta >= 0` (buyers in control). Prefer clearly
   positive delta; be skeptical if delta is negative while you're trying to buy.
5. **Location is sane**: you are not buying directly into the `swing_high` resistance
   with no room to the target. There must be at least ~1R of room before obvious
   resistance.

**Short setup** is the exact mirror: downtrend, bar tags the fast EMA from below,
closes back below it as a bearish bar, `recent_delta <= 0`, room to the downside.

## Brackets (every trade)

- **Stop**: `1.5 × ATR` from entry (converted to ticks). This is mandatory.
- **Target**: `2.0 × ATR` from entry (≈ 1.33R). The bridge sets these as a resting
  stop/target bracket in NinjaTrader the moment you enter.
- You may specify `stop_ticks` / `target_ticks` explicitly; if you omit a stop, the
  bridge injects a default — but you should always set one deliberately.

## Trade management (when already in a position)

- The resting bracket handles the normal stop-out and target. **Let it work.**
- **Discretionary early exit** is allowed only when the thesis is invalidated:
  - Long: trend flips to `down`, OR delta turns clearly negative AND price closes
    back below the slow EMA. → `EXIT`.
  - Short: the mirror. → `EXIT`.
- Do **not** move your stop further away. You may exit early; you may not add risk.
- Never flip directly from long to short in one step. Exit, then re-evaluate next bar.

## Most bars are WAIT

This setup appears a handful of times per session. If conditions are not cleanly met,
the correct action is **WAIT**. Overtrading is the fastest way to fail the daily goal.
