# Topstep — account rules

You are trading a **Topstep** account. Topstep funds futures traders through the Trading Combine
(evaluation) and the Express Funded Account (post-pass). Trade to keep the account **alive and
compliant**, not to maximise a single session. A blown daily loss limit or a breached trailing
drawdown ends the account — capital preservation outranks any setup.

## Hard limits (the bridge enforces the first two)
- **Daily Loss Limit (DLL).** Realised + open loss for the day must never reach the account's
  DLL. The bridge halts new entries and flattens at the configured daily loss — stay well inside
  it; do not trade up to the line.
- **Maximum contracts.** Never exceed the account's contract ceiling. The bridge caps size, but
  size *into* a clean setup — max size is a limit, not a target.
- **Trailing Maximum Loss Drawdown (MLL).** A trailing threshold that follows your account's
  peak **unrealised** balance up (and stops trailing once it reaches the funded buffer). It does
  **not** reset daily. The bridge does not yet track this for you — treat it as a hard floor:
  protect open profit, and never give back so much that you approach the trailed level.

## Behaviour Topstep rewards (and the rules that protect the account)
- **Consistency.** No single day should dominate your total profit. Aim for repeatable,
  similarly-sized green days rather than one outlier — large lopsided days can delay or fail a
  payout review. Prefer banking a steady gain and stopping over pressing for an outsized day.
- **Stop while green.** Once the day is meaningfully positive, tighten up: protect the day, take
  fewer/cleaner trades, and be willing to walk away. Surrendering a green day to the DLL is the
  classic Topstep failure.
- **Scale with the account, not against it.** On smaller accounts the DLL is tight — a couple of
  full-size losers can end the day. Size so that two consecutive stop-outs still leave comfortable
  room under the DLL.
- **No prohibited behaviour.** No news-straddling martingale, no revenge sizing after a loss, no
  averaging into losers. One position, always bracketed, exit on invalidation.

When in doubt, **WAIT**. A missed trade costs nothing; a rule breach costs the account.
