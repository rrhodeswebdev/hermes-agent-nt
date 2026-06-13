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

## Definitions (provided to you each bar in `context`)

- `trend` — `up` when fast EMA > slow EMA, `down` when fast EMA < slow EMA. A crude
  filter only; your structural read overrides it (see market-regime.md).
- `ema_fast`, `ema_slow` — the moving averages (defaults 9 / 21).
- `atr` — Average True Range; your unit of "normal" movement.
- `recent_delta` — cumulative order-flow delta over the recent window (order-flow.md).
- `swing_high`, `swing_low` — the last confirmed pivots (structure).

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

## Bias toward taking clean setups (RELAXED test profile)

> ⚠️ This is a **relaxed, testing-only** posture to exercise the agent end-to-end and
> generate more entries on Sim. It is intentionally less selective than production
> rules. Revert this section before trading anything that matters.

While testing, treat each playbook's *flow* and *location/room* conditions as
preferences rather than hard gates: when the regime is right and the playbook's core
structure + trigger are present, you may enter even if order flow is only neutral or
the location is less than ideal. You still WAIT when the regime is unclear, the
structure is absent, or the trigger bar has not closed. Flow that is *clearly against*
the trade remains a veto. The hard rules above are never relaxed.
