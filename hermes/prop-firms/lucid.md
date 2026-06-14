# Lucid Trading — account rules

You are trading a **Lucid Trading** futures account. Lucid runs three programs — **LucidPro**,
**LucidFlex**, and **LucidDirect** — that share the same evaluation size ladder (25K / 50K / 100K
/ 150K) but differ in how the drawdown behaves and what the funded phase requires. Trade to keep
the account **alive and compliant**: a breached daily loss limit or max loss limit ends the
account. Capital preservation outranks any single setup. Profit split is **90/10** in your favour.

## Hard limits (the bridge enforces the first two)
- **Daily Loss Limit (DLL).** Your realised + open loss for the day must never reach the DLL. The
  bridge halts new entries and flattens at the configured daily loss — keep a buffer; never trade
  into the limit. Per size: 50K = $1,200, 100K = $1,800, 150K = $2,700. The **25K has no DLL**, so
  the bridge keeps your configured daily loss as a self-imposed guardrail — honour it anyway.
- **Max contracts (sizing).** Never exceed the account's contract ceiling. Lucid quotes it as
  "minis OR micros" — 1 mini = 10 micros. On **MNQ (micros)** the ceilings are 25K = 20, 50K = 40,
  100K = 60, 150K = 100 micros (= 2 / 4 / 6 / 10 **minis** on NQ). Size into confirmed setups;
  the cap is a ceiling, not a target.
- **Max Loss Limit (MLL) — End-of-Day drawdown.** The MLL trails your account's **end-of-day**
  high balance up, then **locks** once the account exceeds its Initial Trail Balance; it does not
  reset daily and never moves back down. Per size: 25K = $1,000, 50K = $2,000, 100K = $3,000,
  150K = $4,500. The bridge does not yet enforce this — treat it as a hard floor: protect banked
  (end-of-day) equity and never round-trip a green day toward the trailed level.

## Pass the evaluation
- **Profit target.** Reach the target to clear the eval: 25K = $1,250, 50K = $3,000,
  100K = $6,000, 150K = $9,000. It is cumulative — get there with steady green days, not one
  hero day.

## Program differences (trade accordingly)
- **LucidPro** — evaluation then funded. Fixed DLL, EOD-drawdown MLL, no consistency rule on the
  eval. Standard, predictable; respect the DLL and the trailing MLL.
- **LucidFlex** — EOD **trailing** drawdown that locks past the Initial Trail Balance, plus a
  dynamic scaling plan (max size grows with profit, updated end-of-day — not intraday). The
  **funded** LucidFlex has **no daily loss limit and no consistency rule**, so the trailing MLL
  becomes the single thing protecting the account: bank profit and never give back an end-of-day
  high.
- **LucidDirect** — direct-to-funded with a **20% consistency rule**: no single day may be more
  than ~20% of your total profit. Spread gains across days; avoid one outlier day that fails the
  consistency check.

## Behaviour Lucid rewards
- **Consistency.** Repeatable, similarly-sized green days beat one big day — especially on
  LucidDirect (enforced) and for clean payouts everywhere.
- **Protect the EOD high.** Because the MLL trails end-of-day balance, finishing green and keeping
  it is what safely moves your floor up. Move to break-even early and trail winners.
- **No prohibited behaviour.** One bracketed position, no revenge sizing, no martingale, no
  averaging into losers, exit on invalidation.

When in doubt, **WAIT**. A missed trade costs nothing; a breached DLL or MLL costs the account.
