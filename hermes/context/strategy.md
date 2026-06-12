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
4. **Order flow is not clearly against you** (RELAXED for testing): you do not need
   strongly positive `recent_delta`. Only veto the long when delta is *clearly*
   negative (strong selling). Mildly negative or flat delta is acceptable.
5. **Location is reasonable** (RELAXED for testing): prefer some room to the target,
   but you may take the entry even when `swing_high` is fairly close — treat location
   as a preference, not a hard gate, while testing.

**Short setup** is the exact mirror: downtrend, bar tags the fast EMA from below,
closes back below it as a bearish bar, `recent_delta <= 0`, room to the downside.

## Brackets (every trade)

- **Stop**: `1.5 × ATR` from entry (converted to ticks). This is mandatory.
- **Target**: `2.0 × ATR` from entry (≈ 1.33R). The bridge sets these as a resting
  stop/target bracket in NinjaTrader the moment you enter.
- You may specify `stop_ticks` / `target_ticks` explicitly; if you omit a stop, the
  bridge injects a default — but you should always set one deliberately.

## Armed plans (preferred entry mechanism, when ARM_PLAN is available)

Your decision takes 25–115 s to reach the market. An immediate `ENTER_*` is a MARKET
order sent after that delay — it chases price and may be dropped as stale. When the
setup is valid, prefer **`ARM_PLAN`**:

- `plan.direction` — LONG or SHORT (with the trend, as always).
- `plan.entry_low` / `plan.entry_high` — a tight zone at the price you actually want
  (for a long pullback: around the fast-EMA tag; the zone top is where you'd buy).
- `plan.ttl_bars` — patience in bars (3–5 typical, 10 max).
- `stop_ticks` / `target_ticks` — exactly as for a normal entry (the stop is mandatory).

The bridge rests a **limit order at the zone edge** (long: `entry_high`, short:
`entry_low`): it fills at your price or not at all — zero decision latency at the
trigger. The plan auto-cancels on TTL expiry, on a bar CLOSE through the far side of
the zone (thesis broken), or on a session halt. While a plan is armed you are not
consulted; normal per-bar decisions resume once it resolves. One plan at a time.

## Trade management (when already in a position)

- The resting bracket handles the normal stop-out and target. **Let it work.**
- **Discretionary early exit** is allowed only when the thesis is invalidated:
  - Long: trend flips to `down`, OR delta turns clearly negative AND price closes
    back below the slow EMA. → `EXIT`.
  - Short: the mirror. → `EXIT`.
- Do **not** move your stop further away. You may exit early; you may not add risk.
- Never flip directly from long to short in one step. Exit, then re-evaluate next bar.

## Bias toward taking clean setups (RELAXED test profile)

> ⚠️ This is a **relaxed, testing-only** profile to exercise the agent end-to-end and
> generate more entries on Sim. It is intentionally less selective than the production
> rules. Revert this section (and the order-flow/location relaxations above) before
> trading anything that matters.

When the **core** conditions are met — right trend, a real pullback that tags the fast
EMA, and a same-direction resumption bar — you may **ENTER** even if order flow is only
neutral or location is less than ideal. You still WAIT when the trend is wrong, there is
no pullback, or the bar is not a resumption. The resting `1.5×ATR` stop bracket caps the
downside of each test trade.
