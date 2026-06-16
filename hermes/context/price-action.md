# Knowledge — Price Action

Price action is reading the market from **price and structure alone**: trend, swings,
and *location*. It is your **context** layer — it decides *where* a trade is even
allowed. Order flow then confirms *whether* to take it.

## Trend & structure

- **Trend** — a series of higher highs / higher lows (up) or lower highs / lower lows
  (down). This is read from the **swing structure** itself (the `regime`/`trend` fields,
  with `recent_pivots` the evidence) — no moving averages. Prefer trading **with** the
  trend; in a balanced range, trade the edges; take a counter-trend turn only once
  structure has actually broken AND order flow confirms the new side — never on anticipation.
- **Swings / pivots** — `swing_high` and `swing_low` are the last confirmed turning
  points, and `recent_pivots` is the recent chain. They mark **structure** and the
  **support/resistance** that matters most.
- **Pullback vs reversal** — a *pullback* is a counter-trend pause that holds above
  (uptrend) the prior higher-low; a *reversal* breaks structure.
  Always know which one you're in: trade pullbacks with the trend, break/reclaim or fade
  the levels that define a range, and treat a reversal as tradeable only after the break
  is confirmed — do not pre-empt it.
- **On a strong trend, continuation IS the trade.** The move pays you for staying with it,
  not for fading it. Take the resumption — a shallow pullback that holds (2–5 bars, above
  the prior higher-low up / below the prior lower-high down) and resumes, or a **break-and-
  go** through the level that just gave way — entering where price *is*, not at a far level
  you wish it would retrace to (a deep counter-trend bounce often never comes in a trend
  that keeps extending). Fades wait for a reversal that structure must confirm first.

## Location is everything

The same candle is a great trade at one price and a terrible one at another.

- **Buy location (long)**: price has pulled back **to value** — the prior higher-low /
  a recent support shelf — inside an uptrend, with **room** up to the next resistance
  (`swing_high`) of at least ~1R.
- **Sell location (short)**: mirror — a bounce **to value** in a downtrend with room
  down to the next support.
- **Other valid locations**: a **breakout** through a level that had been capping price
  (with room to the next level), a **reclaim** back through a level that just rejected
  price, or a **range edge** to fade. The rule is unchanged — enter where the move has
  **room** (≥ ~1R) to the next structure.
- **Bad location**: entering extended/chasing (far from value with the move already run),
  or directly into the level you're targeting with no room. **WAIT** for better location.

## Candle triggers (the confirmation bar)

- The trigger is a **decisive bar that confirms the setup**: a pullback tagging the prior
  higher-low (support) then closing back above it (resumption); a bar **breaking and
  closing beyond** a level (breakout / reclaim); or a **rejection bar** — wick into the
  level, close back inside — at a range edge you're fading. Mirror for shorts.
- Prefer a decisive close (body in the direction of the trade), not a tiny doji.
- Avoid triggers immediately after a **climactic** bar (huge range/volume) — wait for
  it to settle.

## Putting it together (your decision frame)

1. **Regime & structure?** Trend, range, or transition — and where are the swings and
   levels (`recent_pivots`, `swing_high`/`swing_low`)? If structure is unreadable → WAIT.
2. **Location?** Is there a real setup at a good price — pullback to value, breakout level
   with room, reclaim, or range edge — with ≥ ~1R to the next structure? Extended or into
   the level you're targeting → WAIT.
3. **Trigger?** Did a decisive bar confirm it (resumption / break-and-close / rejection)?
   If not → WAIT.
4. **Confirmation?** (order-flow.md) Does delta back the move (or show absorption /
   exhaustion if you're fading)?
5. **Risk/goal OK?** (risk-management.md, daily-goal.md) Then act.
