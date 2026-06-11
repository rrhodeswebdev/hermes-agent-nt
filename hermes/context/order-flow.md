# Knowledge — Order Flow

Order flow is the study of **who is in control: buyers or sellers** — the actual
transacting pressure behind price, not just where price went. It is your
**confirmation** layer. Price-action tells you *where*; order flow tells you *whether
the move has force behind it*.

## Core concepts

- **Delta** = (volume traded at the ask) − (volume traded at the bid). Positive delta
  means aggressive buyers are lifting offers; negative means aggressive sellers are
  hitting bids. The bridge gives you `recent_delta` (cumulative delta over a recent
  window). When the feed lacks bid/ask volume, it approximates delta from where each
  bar closes within its range × volume (a close near the high ⇒ buying pressure).
- **Cumulative delta** — the running sum of delta. Rising cumulative delta in an
  uptrend = healthy participation. **Divergence** (price makes a new high but
  cumulative delta does not) warns the move is losing force.
- **Absorption** — large resting limit orders absorbing aggressive flow: price stalls
  despite heavy delta in one direction. Often precedes a reversal of that short-term push.
- **Exhaustion** — a climactic surge of delta into a level that then fails to extend.
  A reason to NOT chase, and sometimes to exit.

## Session matters — judge delta relative to volume

`recent_delta` is **volume-weighted**, so the same conviction prints a much smaller number
overnight. Each bar is tagged with `session`:

- **RTH** (09:30–16:00 ET) — the regular cash session, heavy volume; deltas run large.
- **ETH** — overnight / extended hours, often a *fraction* of RTH volume; deltas run small.

**Do not demand RTH-sized delta in ETH** — a −100 overnight can mean what a −3,000 means
midday. Use **`delta_ratio`** (net delta ÷ recent volume, ≈ −1…+1) as the session-independent
read of force: it answers *"what fraction of recent flow was one-sided"* regardless of how
much volume printed. Rough guide: |`delta_ratio`| ≳ 0.15–0.20 = genuine one-sided pressure;
near 0 = balanced / absorptive. Lean on `delta_ratio` for **force**, and on `recent_delta` +
`session` for raw magnitude and context.

## How you use it in this strategy

You are a **with-trend pullback** trader, so you use order flow to confirm that the
**resumption** of the trend is real:

- **Buying a pullback (long)**: you want `recent_delta >= 0` and ideally turning up as
  price reclaims the fast EMA. Buyers stepping back in on the dip is the green light.
  If delta is firmly negative while price ticks up, the bounce is weak — **WAIT**.
- **Selling a bounce (short)**: mirror — you want `recent_delta <= 0`, sellers
  re-engaging as price fails back below the fast EMA.

## Confirmation checklist (long; mirror for short)

1. Trend up and price has pulled back to the fast EMA (location — see price-action.md).
2. `recent_delta >= 0` — buyers present, not absent.
3. The trigger bar closes back up through the EMA (resumption).
4. No obvious **absorption/exhaustion** against you at the level you're entering.

## Red flags (reasons to WAIT or EXIT)

- **Delta divergence** with your position (new price extreme, weaker delta).
- **Flat/contrary delta** on your entry trigger — the move lacks force.
- A **delta spike into a level** that immediately stalls (absorption) — don't chase.

Order flow is confirmation, not a standalone signal. No trend + location setup ⇒ no
trade, regardless of delta.
