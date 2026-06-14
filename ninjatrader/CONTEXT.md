# CONTEXT — `ninjatrader/` (the NinjaScript strategy, C#)

> Routed from the root [`CLAUDE.md`](../CLAUDE.md). Read that first for project-wide
> identity, rules, and naming. This file is the local map for the NinjaTrader workspace.

## What this workspace is

`HermesBridgeStrategy.cs` is the Windows / NinjaTrader 8 side of the system: the market
interface and order executor. It streams chart bars to the Python `hermes-bridge`, executes
the **risk-approved** orders the bridge returns (with a resting stop/target bracket), and
reports fills. It is **not** a safety authority — all sizing, stops, and limits are enforced by
the bridge's `RiskGate` before any command reaches this strategy.

This file compiles **inside** NinjaTrader against your installed NT8 assemblies; it is not part
of the Python build. There is no compiler in this repo — verify by compiling in the NinjaScript
Editor (F5).

## What it does

| Event | Action |
| --- | --- |
| Historical → realtime transition | `POST /ingest/history` with every loaded bar (if `SendHistory`). |
| Each closed realtime bar | `POST /ingest/bar`, then `GET /commands/next`. |
| Command returned | `EnterLong/EnterShort` (+ bracket) or exit, on the strategy thread via `TriggerCustomEvent`. |
| Fill | `POST /ingest/fill` from `OnExecutionUpdate`. |

Uses `Calculate.OnBarClose` — exactly one decision per closed bar.

## Strategy parameters (set in the NT Strategies dialog — C# PascalCase)

- `BridgeHost` / `BridgePort` — where the bridge is reachable (`127.0.0.1:8787` same box).
- `StrategyId` — **must match** `strategy_id` in `config/trading.yaml`.
- `SendHistory` — bulk-upload loaded bars on start.
- `UseAgentStrategies` — **true** (default): brain authors its own playbook from this chart's
  history. **false**: trades your `hermes/context/strategies/{trending,ranging}/` files. Reported
  to the bridge before history so the study runs in the right mode. Overrides `strategies.source`.
- `AllowLive` — leave **false**. The strategy refuses non-Sim/Playback accounts unless true.
- `PropAccount` — **single dropdown** of valid firm·type·size combos (the `PropFirmAccount` enum,
  e.g. *Lucid Trading - LucidPro - 50K*; `(none)` = nothing selected). A flat enum is used on
  purpose: cascading dependent dropdowns are unreliable in NT's grid (dependent lists came back
  empty), whereas a native enum always populates and only offers valid combos. Reported to the
  bridge over `/ingest/account`; the bridge loads that firm's context file into the brain and
  **enforces** the account's daily-loss + max-contracts limits. Runtime only (not persisted, like
  the account name). The enum + `PropFirmAccounts.Map` (bottom of the .cs) **must mirror**
  `config/prop-firms.yaml` — the bridge owns the numbers and validates the combo.
- Dashboard button knobs: `RefreshSeconds`, `FontSize`.

## On-chart dashboard

Draws a draggable **"HERMES — DASHBOARD"** button (status dot: green reachable / amber
connecting / red offline; polls `GET /health` only). Click opens the bridge's full HTML
dashboard in an **embedded WebView2** NinjaTrader window (loaded by reflection, so it
compiles/runs without WebView2 — falls back to the default browser). The same dashboard is
served at `http://<bridge-host>:8787/`. S/R levels are rendered by that HTML dashboard, not
by this on-chart component — the strategy no longer draws them on the chart.

## Working rules (local)

- The account is chosen **in NinjaTrader**, reported to the bridge automatically; the bridge's
  `execution.account` is only a fallback until the strategy connects.
- Keep the JSON shapes in lockstep with `bridge/hermes_bridge/models.py` — that file is the
  contract. If you change an endpoint or payload here, update the bridge (and its
  [`CONTEXT.md`](../bridge/CONTEXT.md)) together.
- Sim-first: never enable `AllowLive` as part of routine work.
