# Risk Management — Hard Rules

These rules keep you in the game. The bridge **enforces** them server-side: it will
clamp your size or reject any order that violates them. Internalize them so you don't
even propose unsafe trades. (Numbers below are the defaults in
`config/trading.yaml` — always trust `nt_session_status` for the live values.)

## Per-trade rules

- **Always use a protective stop.** Every entry carries a stop (≈ 1.5 × ATR). An entry
  without a stop is auto-rejected / auto-stopped by the bridge.
- **Risk a fixed small amount per trade.** Default cap: **$250 risk per trade**. Your
  size is chosen so `stop_distance × tick_value × contracts ≤ max_risk_per_trade`. If a
  single contract's stop risks more than the cap, the trade is rejected — your stop is
  too wide for this instrument; **WAIT**.
- **Position cap.** Never exceed **max_contracts** (default 2). The bridge clamps size.
- **One position at a time.** No pyramiding, no averaging down, no flipping in a single
  step. Entries are only allowed when **flat**.

## Daily rules (see daily-goal.md)

- **Max trades/day** (default 10). After that, no new entries.
- **Max daily loss** (default $400). If realized P&L for the day reaches −$400, the
  bridge **flattens and halts** you for the day.
- A trade whose worst-case stop-out would push the day past the max-loss limit is
  **rejected** before it's taken.

## Behavioral rules

- **No revenge trading.** A loss is data, not a debt to recover. The next setup is
  judged on its own merits.
- **No chasing.** If you missed the clean entry, do not enter extended. WAIT for the
  next setup.
- **When uncertain, WAIT.** WAIT is a position. It has zero risk and is often correct.
- **Respect the gate.** If the bridge clamps or rejects your order, that is the system
  working. Do not try to route around it.

## R multiples

Think in **R** (1R = your stop distance in dollars), not ticks or dollars in
isolation. Target ≈ 1.33R (2.0 ATR vs 1.5 ATR stop). A string of small losses is
survivable; one oversized loss is not. Protect R.
