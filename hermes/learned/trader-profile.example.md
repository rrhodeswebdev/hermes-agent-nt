# Trader Profile

> The agent's model of the trader. Reflection updates this from your feedback (Plan 3).
> Keep it short; it is injected into every decision.
>
> This is the shipped TEMPLATE. Copy to `trader-profile.md` (gitignored) to seed your
> own; the live file stays local and is never pushed.

- Account: Sim only while validating. Never assume live.
- Risk posture: conservative — respect the configured per-trade and daily limits; when
  in doubt, WAIT. One position at a time (no pyramiding).
- Style: trend-pullback with order-flow confirmation on MNQ. Patience over activity —
  most bars are WAIT.
