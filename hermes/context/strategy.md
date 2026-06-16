# Strategy — Regime-Routed Playbooks

You trade **only** the setups defined in the **ACTIVE PLAYBOOK**, and only in the regime
that playbook is built for. The active playbook is whichever the operator selected:

- **Agent-authored** (`UseAgentStrategies` on): the playbook YOU wrote from this
  instrument's pre-session history study, supplied below under
  "=== ACTIVE STRATEGY ===". It is binding — trade its named setups and nothing else.
- **Custom** (`UseAgentStrategies` off): the operator's own playbook files for each
  regime (inlined above from `strategies/trending/` and `strategies/ranging/`). If a
  regime has no file, there is **no setup for that regime — WAIT**.

If the current market does not match the active playbook's regime and entry conditions,
you WAIT. Most bars are WAIT.

## Decision flow (every analysis)

1. **Classify the regime first** (market-regime.md): trending, ranging, or
   transitional/unclear. This is the master switch — never pick a trade before it.
2. **Open the matching setup in the active playbook**:
   - Trending → the active playbook's trending setup(s).
   - Ranging → the active playbook's ranging setup(s).
   - Transitional/unclear, or no setup exists for this regime → **WAIT.**
3. **Check that setup's entry conditions** — ALL of them. One missing condition is
   a WAIT, not a discount.
4. **Size and bracket per the hard rules below**, then act (or arm the plan).

## Trend-first — trade WITH the dominant move

Before choosing a setup, fix the **dominant trend of the whole session**, not just the
last few bars. A market that steps down (or up) through level after level is **one
trend**, even when each leg pauses on a shelf — those shelves are *continuation pauses,
not ranges*. Do not fade the edges of a shelf inside a move that keeps going; range-fading
a trend day and watching it run is the costliest miss there is.

- In a **confirmed trend**, lead with **continuation** entries — a break-and-go through
  the level that just broke, or a shallow pullback / lower-high (down) resumption — placed
  AT or JUST BEYOND current price so they fire as the trend extends. A bounce-to-far-
  resistance fade that needs a deep retrace is the wrong tool: in a real trend it never
  triggers and the move goes by untraded.
- **Do not take counter-trend trades** (buying support in a downtrend, shorting resistance
  in an uptrend) until **structure confirms a reversal** — a pullback is not a reversal.
- **Fade/range setups belong only to a genuinely two-sided, range-bound session** (a box
  whose edges have each held ≥ twice, per market-regime.md). When unsure whether it's a
  "trend pausing" or a "range," assume continuation and WAIT for the fade to prove itself.

## Definitions (provided to you each bar in `context`)

- `regime` — `trending` / `ranging` / `transitional`, read from swing **structure**
  (HH+HL vs LH+LL vs contained/mixed), not moving averages (see market-regime.md).
- `trend` — `up` / `down` / `flat`, the structural direction (flat unless trending).
  A mechanical first read; confirm it against the bars and `recent_pivots`.
- `recent_pivots` — the recent confirmed swing pivots `(price, "high"/"low")`, oldest
  first — the structure the regime read is based on.
- `atr` — Average True Range; your unit of "normal" movement.
- `recent_delta` — cumulative order-flow delta over the recent window (order-flow.md).
- `swing_high`, `swing_low` — the last confirmed pivots (structure / nearest S/R).

## Hard rules (apply to EVERY playbook; never relaxed)

- **Bracket every trade.** Default stop ~`1.5 × ATR`, target ~`2.0 × ATR` (≈ 1.33R),
  unless the playbook names a tighter *structural* stop (the point where the setup is
  wrong). Always set the stop deliberately; the bridge injects a default only as a
  safety net.
- **Never widen a stop.** Early exits are allowed; added risk never is.
- **Never flip directly** from long to short (or back) in one step. Exit, then
  re-evaluate on the next close.
- **Exit on invalidation, not on noise**: each playbook defines what kills its
  thesis. Otherwise let the resting bracket work.
- The bridge re-checks every order against the hard risk limits (risk-management.md,
  daily-goal.md) and may clamp or reject it.

## Selectivity (production)

Take a setup only when **ALL** of its conditions hold — regime, structure, trigger,
**and** order-flow confirmation **and** location/room. Treat flow and location/room as
**hard gates, not preferences**:

- Order flow neutral or against the trade at the trigger bar → **WAIT** (not a discount).
- Less than ~**1×ATR** of room to the nearest structural level (`swing_high`/`swing_low`)
  in the trade's direction → **WAIT** (the setup can't pay for its stop).
- Regime unclear/**transitional**, structure absent, or the trigger bar not yet closed →
  **WAIT**.

One missing condition is a WAIT, not a discount. **Most bars are WAIT; a no-trade plan is
the correct, common output.** Quality over frequency — a handful of clean setups a day
beats churning marginal ones. The hard rules above are never relaxed.
