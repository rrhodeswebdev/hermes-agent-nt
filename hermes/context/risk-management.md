# Risk Management — Hard Rules

These rules keep you in the game. The bridge **enforces** them server-side: it will
clamp your size, widen/cap your stop, manage the trade, or reject any order that
violates them. Internalize them so you don't even propose unsafe trades. (Numbers below
are the defaults in `config/trading.yaml` — always trust `nt_session_status` for the
live values.)

## Per-trade rules

- **Always use a protective stop — give it room.** Every entry carries a stop. Size it to
  where the trade is **structurally wrong** (beyond the swing that defines the setup), not
  to the nearest tick. The baseline is **≈ 2.0 × ATR**. The bridge then **clamps your stop
  into a band**: never tighter than `min_stop_ticks` (a volatility-noise floor, so a 1-minute
  wick can't tag you out of a trade that was still valid) and never wider than
  `max_stop_ticks` (a spike cap; size shrinks to fit instead). A too-tight stop is the most
  common way to turn a winner into a loss — when in doubt, give it the wider stop and take
  **less size**, never a tight stop with more size.
- **Risk a fixed small amount per trade.** Default cap: **$250 risk per trade**. Your size
  is chosen so `stop_distance × tick_value × contracts ≤ max_risk_per_trade`. A wider stop
  therefore means **fewer contracts**, not more risk. If a single contract's banded stop
  still risks more than the cap, the trade is rejected — **WAIT**.
- **Smaller size in a volatility shock.** When the current ATR spikes to ≥ `shock_ratio` ×
  the baseline ATR, the bridge **scales the per-trade dollar budget down** (default: halved).
  Wild conditions ⇒ smaller size automatically. Don't fight it.
- **Position cap.** Never exceed **max_contracts** (default 2). The bridge clamps size.
- **One position at a time.** No pyramiding, no averaging down, no flipping in a single
  step. Entries are only allowed when **flat**.

## Trade management — protect a winner (the bridge does this for you)

The bridge runs a **deterministic trade manager** on every open position. You do not need
to micromanage exits, and you **cannot loosen** this protection — you can only ever add a
tighter discretionary exit on top of it:

- **Phase 1 — give it room (0 → +1R).** The wide initial stop (above) is the only stop.
  Let the trade breathe; do not bail on noise.
- **Phase 2 — risk off (≥ +1R).** Once price runs **+1R** in your favor (1R = your initial
  stop distance), the stop is pulled to **breakeven**. A trade that worked can no longer
  become a loss.
- **Phase 3 — trail (in profit).** Thereafter the stop **trails behind each new swing**
  (the higher-low in an uptrend, the lower-high in a downtrend), locking in more as
  structure builds. This is how the wider stop pays for itself: room early, protection late.
- **Structural invalidation always exits.** If the close breaks the protective swing or the
  trend flips against you, exit — don't wait for the trailed stop.

Because the manager only **tightens** the stop, the old hard rule still holds: **never move
a live stop further from price.** The band floor is applied at **entry**, never after.

## Daily rules (see daily-goal.md)

- **Max trades/day** (default 20). After that, no new entries.
- **Max daily loss** (default $400). If realized P&L for the day reaches −$400, the bridge
  **flattens and halts** you for the day.
- A trade whose worst-case stop-out would push the day past the max-loss limit is
  **rejected** before it's taken.
- **No self-imposed entry cutoff before the close.** New entries are allowed through the
  whole RTH session. Do **not** invent a "stop entering after HH:MM" rule earlier than the
  final **30 minutes** (15:30 ET) — judge every setup on regime + flow + location, not the
  clock. The only entry halts are the ones above (goal, max-loss, max-trades) and a news
  blackout. In the last 30 min (15:30–16:00 ET) you may stand down from NEW entries and
  manage open positions only; before then there is no time gate.

## Behavioral rules

- **No revenge trading.** A loss is data, not a debt to recover. The next setup is judged
  on its own merits.
- **No chasing.** If you missed the clean entry, do not enter extended. WAIT for the next
  setup — the wider stop makes a chased entry doubly expensive.
- **When uncertain, WAIT.** WAIT is a position. It has zero risk and is often correct.
- **Respect the gate.** If the bridge clamps, widens, or rejects your order, that is the
  system working. Do not try to route around it.

## R multiples

Think in **R** (1R = your initial stop distance in dollars), not ticks or dollars in
isolation. Default target ≈ **1.5R** (3.0 ATR vs 2.0 ATR stop), but with the trail on your
real winners come from **letting a runner trail past the target**, not from a fixed exit.
The math that keeps you green: a wider stop lowers your hit-rate cost only if you (1) size
down to hold R constant and (2) let breakeven + the trail turn your good trades into >1R
wins. A string of small losses is survivable; one oversized loss, or a winner round-tripped
to a loss, is not. **Protect R, then let it run.**
