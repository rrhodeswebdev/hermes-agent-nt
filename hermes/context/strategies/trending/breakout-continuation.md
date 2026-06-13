# Playbook (Trending) — Breakout Continuation

**Regime fit**: trending markets ONLY. The trend's *acceleration* entry: join when a
pause in an established trend resolves in the trend's direction.

## The idea

Within a trend, price pauses in tight consolidations (flags/boxes of overlapping
bars). When the consolidation **breaks in the trend's direction with force**, the next
leg is launching. You are buying acceptance at new prices — the opposite logic of a
fade — so this playbook demands *strong* flow confirmation. Weak breakouts are traps
(the mirror image of strategies/ranging/failed-breakout.md — know which side of that
event you are on).

## Entry conditions (long; mirror for short)

ALL must be true:

1. **Regime**: established uptrend (advancing swings, EMAs separated and rising)
   BEFORE the consolidation formed. This is continuation, not a bottom-call.
2. **A real pause**: ≥ 3–4 overlapping, smaller-range bars holding above/near
   `ema_fast` — a tight flag, not a deep retracement (a deep one is the
   trend-pullback playbook instead, or a warning).
3. **The break**: a decisive bar **closes above** the consolidation's high
   (acceptance — a close, not a wick).
4. **Flow confirms strongly** (order-flow.md): the breakout bar carries clearly
   positive delta. A breakout on flat/negative delta is the trap — hard veto.
5. **Room**: ≥ ~1R to the next opposing structure beyond the breakout point.
6. **Not climactic/exhausted**: the break is not the trend's umpteenth vertical bar
   far from value — late, stretched breaks are exhaustion candidates.

## Bracket

- **Stop**: below the consolidation's low — if price re-enters and traverses the
  whole pause, the breakout failed. Keep total stop ≲ 1.5 × ATR; if the
  consolidation is taller than that, the setup is too sloppy — skip.
- **Target**: ~2 × ATR, or in front of the next structure if closer.

## Management

- Breakouts resolve fast. If the breakout bar's gains are given back and price
  **closes back inside** the consolidation, the trade is invalidated — exit; do not
  wait for the full stop.
- Otherwise let the bracket work. Never widen the stop.

## Skip it when

- There was no real trend before the pause (that's a range breakout — different odds;
  no playbook here takes it on the first break).
- The "break" is a wick without a closing body beyond the level.
- Delta does not expand with the break (trap signature).
- The consolidation sits directly under a major opposing level (no room).
- ATR has gone climactic — late vertical trends end in exhaustion, not continuation.
