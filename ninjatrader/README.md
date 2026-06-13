# NinjaScript — HermesBridgeStrategy

`HermesBridgeStrategy.cs` is the NinjaTrader 8 side of the system. It streams chart
data to the Python `hermes-bridge` and executes the risk-approved orders the bridge
returns, on whatever account is selected in the strategy — a simulated **Sim** or
**Playback** account by default (it refuses a live account unless `AllowLive` is set),
and it reports that selection to the bridge so the dashboard/logs follow it. It **also
renders the agent dashboard
card + S/R levels directly on the chart** — the dashboard is built into the strategy
(NT8 strategies support `OnRender`), so there is no separate indicator to add.

## Install

1. Open NinjaTrader 8 → **New → NinjaScript Editor**.
2. In the **Strategies** folder, right-click → **New Strategy** (or **Import** and
   point at this file). Easiest: open this file's contents and paste it into a new
   strategy named `HermesBridgeStrategy`, then **Compile** (F5).
   - Alternatively, copy this file to
     `Documents/NinjaTrader 8/bin/Custom/Strategies/HermesBridgeStrategy.cs` and
     compile from the editor.
3. Fix any compile output in the editor (it links against your installed NT8
   assemblies).

## Run

1. Start the bridge first: `hermes-bridge serve` (see `../bridge/README.md`).
2. Open a chart for your instrument + timeframe (e.g. ES 12-25, 5-minute).
3. **Right-click chart → Strategies…**, add **HermesBridgeStrategy**, and set:
   - `BridgeHost` / `BridgePort` → where the bridge is reachable
     (`127.0.0.1:8787` if same machine / WSL2; the bridge LAN IP if the agent runs
     on another box).
   - `StrategyId` → must match the bridge's `strategy_id` in `config/trading.yaml`.
   - `SendHistory` → true to bulk-upload loaded bars on start.
   - `AllowLive` → leave **false**; the strategy refuses to trade a live (brokerage)
     account unless this is explicitly true. Simulated **Sim*** and **Playback** accounts
     always trade with it off.
4. Select your account (**Sim101** or a **Playback** account) and enable the strategy.
   The strategy reports the selected account to the bridge automatically, so the
   dashboard, `/health`, and logs follow your choice — there's nothing to set in the
   bridge config (its `execution.account` is only a fallback until the strategy connects).

## What it does

| Event | Action |
|-------|--------|
| Historical → realtime transition | `POST /ingest/history` with every loaded bar |
| Each closed realtime bar | `POST /ingest/bar`, then `GET /commands/next` |
| Command returned | executes `EnterLong/EnterShort` (+ stop/target bracket) or exit, on the strategy thread via `TriggerCustomEvent` |
| Fill | `POST /ingest/fill` from `OnExecutionUpdate` |

Order sizing, stops, daily goal, and all risk limits are enforced by the bridge's
`RiskGate` before a command ever reaches this strategy. See `../docs/SAFETY.md`.

> Timeframe note: the strategy uses `Calculate.OnBarClose`, so the agent is asked to
> decide once per closed bar. Pick a timeframe (5m default) that matches how often
> you want it reasoning. Sub-minute bars will call the agent very frequently.

---

## Built-in dashboard — see what the agent is doing

The strategy draws a live **dashboard card** on the chart (position, P&L, trades,
daily-goal status, **data age** so you can spot a delayed feed, the last decision + its
rationale, recent decisions, and the armed plan) plus the agent's **support/resistance
lines**. It polls the bridge's pre-formatted panels (`GET /panel.txt` + `GET /levels.txt`),
so there's no JSON parsing in C#. The card is **click-draggable** (double-click resets);
the header glyph folds it to a compact strip. Position/fold persist with the workspace.

Dashboard knobs on the strategy: `RefreshSeconds`, `FontSize`, `RecentRows`, `ShowLevels`.

> **Upgrading from the separate `HermesDashboard` indicator?** It's now folded into the
> strategy. **Remove the HermesDashboard indicator from your charts** and delete it from
> `Documents/NinjaTrader 8/bin/Custom/Indicators/` so you don't get two cards.

Prefer a browser? The bridge also serves a full **web dashboard** at
`http://<bridge-host>:8787/` (auto-refreshing) — open it on the Mac or inside the VM.
