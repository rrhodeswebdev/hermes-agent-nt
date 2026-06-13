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

Whatever setup you take — a pullback resuming, a breakout, a level reclaim, a
failed-breakout fade, a range-edge rejection — order flow is the gate that confirms real
participation is behind the trade. The delta you want depends on whether you are JOINING a
move or FADING one:

- **Joining a move (long)** — a pullback resuming up, a breakout above resistance, a
  reclaim of a level: you want `recent_delta >= 0` (ideally turning up / expanding) as
  price pushes your way. Aggressive buyers showing up on your trigger is the green light;
  firmly negative delta while price ticks up is a weak move — **WAIT**.
- **Joining a move (short)**: mirror — you want `recent_delta <= 0`, sellers engaging.
- **Fading a move** (a failed breakout, a range edge holding): you want the pushing side
  to be **absorbed or exhausted** — a delta surge into the level that fails to extend, or
  divergence — confirming the aggressors are trapped. Never fade a move that delta still
  backs with force.

## Confirmation checklist (long; mirror for short)

1. The setup has clean **location/structure** (see price-action.md): a pullback to value,
   a breakout level with room, a reclaimed level, or a range edge.
2. Delta agrees with the trade — present in your direction when **joining** a move;
   absorbed / exhausted / divergent against the move when **fading** one. Read force with
   `delta_ratio`, magnitude with `recent_delta` + `session`.
3. The **trigger bar** confirms — a decisive close in your direction at/through the level.
4. No obvious **absorption/exhaustion** against you at the level you're entering.

## Red flags (reasons to WAIT or EXIT)

- **Delta divergence** with your position (new price extreme, weaker delta).
- **Flat/contrary delta** on your entry trigger — the move lacks force.
- A **delta spike into a level** that immediately stalls (absorption) — don't chase.

Order flow is confirmation, not a standalone signal. No structure/location setup ⇒ no
trade, regardless of delta.
