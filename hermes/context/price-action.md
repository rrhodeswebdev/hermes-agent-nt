# Knowledge — Price Action

Price action is reading the market from **price and structure alone**: trend, swings,
and *location*. It is your **context** layer — it decides *where* a trade is even
allowed. Order flow then confirms *whether* to take it.

## Trend & structure

- **Trend** — a series of higher highs / higher lows (up) or lower highs / lower lows
  (down). You only trade **with** the trend. The fast/slow EMA relationship
  (`ema_fast` vs `ema_slow`) is your objective trend filter.
- **Swings / pivots** — `swing_high` and `swing_low` are the last confirmed turning
  points. They mark **structure** and the **support/resistance** that matters most.
- **Pullback vs reversal** — a *pullback* is a counter-trend pause that holds above
  (uptrend) the prior higher-low / the moving average. A *reversal* breaks structure.
  You trade pullbacks, not reversals.

## Location is everything

The same candle is a great trade at one price and a terrible one at another.

- **Buy location (long)**: price has pulled back **to value** — the fast EMA / a prior
  higher-low — inside an uptrend, with **room** up to the next resistance
  (`swing_high`) of at least ~1R.
- **Sell location (short)**: mirror — a bounce **to value** in a downtrend with room
  down to the next support.
- **Bad location**: entering far from the EMA (extended/chasing), or directly into the
  opposing swing level with no room to the target. **WAIT** for better location.

## Candle triggers (the resumption bar)

- A pullback that **tags the fast EMA** and then prints a **bullish bar closing back
  above it** (uptrend) is your trigger. The mirror bearish bar is the short trigger.
- Prefer a decisive close (body in the direction of the trade), not a tiny doji.
- Avoid triggers immediately after a **climactic** bar (huge range/volume) — wait for
  it to settle.

## Putting it together (your decision frame)

1. **Trend?** If not clearly up or down → WAIT.
2. **Location?** Has price pulled back to value (the fast EMA) with room to target? If
   extended or into resistance → WAIT.
3. **Trigger?** Did the bar tag the EMA and close back through it in the trend
   direction? If not → WAIT.
4. **Confirmation?** (order-flow.md) Does delta support it?
5. **Risk/goal OK?** (risk-management.md, daily-goal.md) Then act.
