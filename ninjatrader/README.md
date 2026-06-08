# NinjaScript — HermesBridgeStrategy

`HermesBridgeStrategy.cs` is the NinjaTrader 8 side of the system. It streams chart
data to the Python `hermes-bridge` and executes the risk-approved orders the bridge
returns, on the **Sim** account by default.

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
   - `AllowLive` → leave **false**; the strategy refuses to trade a non-Sim account
     unless this is explicitly true.
4. Select the **Sim101** account, enable the strategy.

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

## HermesDashboard (Indicator) — see what the agent is doing

`HermesDashboard.cs` is an **indicator** that shows the agent's live state as a panel
on the chart: position, P&L, trades, daily-goal status, **data age** (so you can spot a
delayed feed), the last decision + its rationale, and recent decisions. It polls the
bridge's pre-formatted panel (`GET /dashboard.txt`), so there's no JSON parsing in C#.

Install: NinjaScript Editor → **New → Indicator** → paste `HermesDashboard.cs` →
**Compile (F5)**. Then **right-click any chart → Indicators… → HermesDashboard**, set
`BridgeHost`/`BridgePort` (same as the strategy), and add it. Drop it on the trading
chart or a **separate chart window** — it only needs network access to the bridge.

Prefer a browser? The bridge also serves a full **web dashboard** at
`http://<bridge-host>:8787/` (auto-refreshing) — open it on the Mac or inside the VM.
