# Daily Goal & Session Discipline

You trade to a **daily plan**, not to "make as much as possible." Consistency over
days beats a big day followed by a blow-up. The bridge tracks and enforces this; check
`nt_session_status` for live numbers.

## The two numbers that end your day

1. **Daily profit target** (default **+$500** realized). When the day's realized P&L
   reaches the target, **you are done**: the bridge halts new entries and flattens any
   open position. Do not seek "just one more." Green day banked.
2. **Daily max loss** (default **−$400** realized). When reached, the bridge
   **flattens and halts** for the day. This is non-negotiable and protects your capital
   and your judgment. Stop. Come back tomorrow.

When halted (for either reason), every bar's answer is **WAIT** unless you still hold a
position that must be closed — and the bridge will close it for you.

## Within the day

- **Trade quality, not quantity.** A few good setups beat many mediocre ones. The
  max-trades cap (default 10) exists to stop death-by-a-thousand-cuts.
- **Protect a green day.** If you're up near the target, tighten your standards — only
  the cleanest setups. Don't give back the day on a sloppy trade.
- **Don't dig a hole deeper.** If you're down on the day, the rules don't change: same
  small risk, same setup criteria. No size-ups to "get it back."

## Daily routine

- **Start of session**: confirm the bridge is connected and the account is **Sim**.
  Note the day's target and max-loss from `nt_session_status`.
- **During**: take only `strategy.md` setups; respect every limit in
  `risk-management.md`.
- **End of session / on halt**: stop trading. Review what worked. The counters reset on
  the next trading day.

> The goal is to **survive and compound**. A trader who never has a catastrophic day
> and books steady green days wins. That is the whole game.
