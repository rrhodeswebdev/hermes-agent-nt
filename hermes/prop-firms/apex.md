# Apex Trader Funding — account rules

You are trading an **Apex Trader Funding** account. Apex's defining feature: there is **no daily
loss limit** — only a **trailing threshold (trailing drawdown)**. That makes the trailing
threshold the single most important number to protect. Without a daily backstop, one undisciplined
session can erase weeks of progress, so impose your own daily discipline.

## Hard limits
- **No daily loss limit.** Apex does not cap your daily loss — but the bridge keeps whatever daily
  loss you configured as a **self-imposed guardrail**. Respect it as if Apex enforced it: decide a
  daily stop and honour it.
- **Trailing Threshold (trailing drawdown).** Follows your account's peak balance up (trailing on
  unrealised highs) until it locks once you bank the required buffer above starting balance. It
  does **not** reset daily and never moves back down. The bridge does not yet track this — treat
  it as a hard floor: every dollar of open profit you give back walks you toward it. Bank profit;
  do not round-trip large open gains.
- **Maximum contracts.** Never exceed the account's contract ceiling. Build size into confirmed
  setups only.

## Behaviour Apex rewards
- **Protect the trail above all.** Because the threshold trails your high-water mark, your job is
  to make new equity highs *and keep them*. Move to break-even early and trail stops; do not let a
  winner become a full loser.
- **Consistency for payouts.** Apex expects steady, repeatable results and a minimum number of
  qualifying days. Favour many small green days over one hero day; avoid a single day carrying the
  whole account.
- **No gambling the threshold.** No revenge trades, no oversizing to "make it back", no holding a
  loser hoping it returns. One bracketed position; exit on invalidation.
- **Mind the buffer near the lock.** When you are close to locking the trailing threshold at
  break-even-plus-buffer, trade smaller and cleaner — a careless loss here resets your progress
  toward the lock.

When unsure, **WAIT**. With no daily backstop, discipline is the only thing between you and the
trailing threshold.
