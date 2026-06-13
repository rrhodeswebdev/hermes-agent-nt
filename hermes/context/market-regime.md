# Knowledge — Market Regime

The regime is the **master switch** of your decision loop. Every market alternates
between **trending** (directional, impulsive) and **ranging** (rotational, mean-
reverting) conditions, with messy **transitions** between them. The same setup that
prints money in one regime bleeds in the other. You never ask "should I buy or sell?"
before you have answered "**what kind of market is this?**"

## What you classify from

You receive each bar: `regime` (trending / ranging / transitional) and `trend`
(up / down / flat) read from swing **structure**, `recent_pivots` (the actual swing
highs/lows the read is built from), `atr` (14), `swing_high`, `swing_low`, `recent_delta`,
and the last ~30 bars of OHLCV. The features are computed over a longer window than the raw
bars show — they are your link to the historical baseline. The `regime`/`trend` labels are
a **mechanical first read** (it compares the last couple of swing highs and lows); confirm
it against the bars and `recent_pivots` yourself, and downgrade to transitional the moment
the structure turns mixed.

## The three regimes

### Trending (up or down)

- **Structure** (the read): a chain of higher highs + higher lows (up) or lower highs +
  lower lows (down) across `recent_pivots`. Swings keep *advancing*, and each pullback
  holds above the prior higher-low (up) / below the prior lower-high (down).
- **Bars**: directional bars with bodies in the trend direction outnumber and out-size
  counter-trend bars; pullbacks are shallow (2–5 bars, overlapping, small bodies).
- **Flow**: `recent_delta` persistently agrees with the direction.
- → Apply the active playbook's **trending** setup(s) (strategy.md).

### Ranging

- **Structure** (the read): swings stop advancing — highs form near prior highs, lows
  near prior lows (a horizontal box). `swing_high` and `swing_low` are the box edges and
  have each been respected at least twice.
- **Bars**: heavy overlap, frequent direction flips, wicks at the edges; pushes toward
  an edge stall instead of extending.
- **Flow**: delta flips sign frequently; strong delta into an edge produces little
  price progress (absorption).
- **Tradeable only if the box is wide enough**: edge-to-edge ≳ 3 × ATR. A narrower box
  cannot pay for its stop — stand aside.
- → Apply the active playbook's **ranging** setup(s) (strategy.md).

### Transitional / unclear (no playbook — WAIT)

- A trend losing force: momentum bars shrinking, pullbacks deepening past prior
  swings, delta diverging at new extremes.
- A range threatening to break: price pressing one edge repeatedly with rising delta.
- **Volatility shock**: current bar ranges far above the recent ATR norm (news,
  open/close auctions) — climactic bars in both directions, no structure.
- Fresh structure break that has not yet built a new pattern (first bars after a
  range breakout or a trend reversal).
- In transition you have **no edge**. WAIT until the market proves its new character —
  typically several bars of clean structure — then trade the NEW regime's playbook.

## Current vs. historical — always anchor the read

Classify the **current** behavior against the longest baseline you have. The raw bars
show the last ~30; `atr`, `recent_pivots`, and the swings summarize the longer window —
use them as the historical anchor:

- **Volatility context**: compare the last few bars' ranges to `atr` (the recent
  norm). Bars running well **below** ATR = compression — inside a range it often
  precedes a breakout (be ready for the failed-breakout vs breakout-continuation
  resolution). Bars suddenly running far **above** ATR = a volatility shock; after a
  long trend leg, expansion usually marks exhaustion, not acceleration.
- **Location in the bigger picture**: where is `last_close` relative to `swing_high`
  and `swing_low` from the longer window? A "trend" in the visible bars that is merely
  travelling from one side of larger structure to the other should be traded with the
  RANGE map in mind — expect a stall at the larger level, take profits earlier.
- **Regime age**: trends that have already run many legs, or ranges tested many times,
  are closer to their end. Demand higher-grade setups (risk-management.md) late in a
  regime's life; be generous to fresh, young regimes.

## Re-classify continuously

The regime is an input EVERY bar, not a morning opinion. The market does not announce
the switch — it shows it in structure first. When evidence turns mixed, downgrade to
transitional and stand down; when it resolves, switch playbooks without loyalty to
yesterday's read. Holding a position when the regime flips against its playbook is an
**invalidation** — manage per the playbook's exit rules.
