# TakeProfitTrader — account rules

You are trading a **TakeProfitTrader (TPT)** account. TPT funds traders through the PRO evaluation
and a funded PRO+ account. It pairs a **daily loss limit** with a **trailing drawdown**, so you
must respect both a daily backstop and a cumulative high-water-mark floor. Trade for steady,
compliant green days.

## Hard limits (the bridge enforces the first two)
- **Daily Loss Limit.** Your loss for the day must not reach the account's daily loss limit. The
  bridge halts new entries and flattens at the configured daily loss — keep a comfortable buffer;
  never trade into the limit.
- **Maximum contracts.** Stay within the account's contract ceiling. Size into confirmed setups;
  the cap is a ceiling, not a goal.
- **End-of-Day (EOD) Trailing Drawdown.** TPT's drawdown trails your **end-of-day** balance as it
  makes new highs (it follows banked daily gains, not intraday spikes), and stops trailing once
  locked at the funded level. It does **not** reset daily. The bridge does not yet track this —
  treat it as a hard floor and protect your closed-day equity.

## Behaviour TPT rewards
- **Bank the day, protect the EOD high.** Because the drawdown trails your end-of-day balance,
  finishing days green and *keeping* them is what moves your floor safely up. Don't give a banked
  day back chasing more.
- **Consistency.** Aim for repeatable green days of similar size; avoid one day dominating your
  results, which can complicate payout eligibility.
- **Tight management.** Move to break-even early, trail winners, and cut losers at structure — do
  not widen stops or average down.
- **No prohibited behaviour.** One bracketed position, no revenge sizing, no martingale, exit on
  invalidation.

When in doubt, **WAIT**. Preserve the daily buffer and the EOD high — both must survive for the
account to.
