# NinjaScript — HermesBridgeStrategy

`HermesBridgeStrategy.cs` is the NinjaTrader 8 side of the system. It streams chart
data to the Python `hermes-bridge` and executes the risk-approved orders the bridge
returns, on whatever account is selected in the strategy — a simulated **Sim** or
**Playback** account by default (it refuses a live account unless `AllowLive` is set),
and it reports that selection to the bridge so the dashboard/logs follow it. It **also
draws the agent's S/R levels on the chart and shows a small on-chart "HERMES —
DASHBOARD" button** that opens the bridge's full HTML dashboard in a NinjaTrader
window (embedded WebView2, with a browser fallback). The rich panel that used to be
drawn as an on-chart card now lives entirely in that HTML dashboard.

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
   - `UseAgentStrategies` → **true** (default): the agent **authors its own playbook**
     from this chart's historical bars (the pre-session study) and trades that. **false**:
     it trades **your own** playbooks under `hermes/context/strategies/{trending,ranging}/`
     and invents nothing — if those dirs are empty it simply WAITs. The toggle is reported
     to the bridge before history so the study runs in the right mode. The agent names each
     setup it authors; the HTML **dashboard** lists them all and highlights the active one —
     the setup the brain declared in its plan, or else the one matching the live regime. See
     the full authored playbook anytime at `GET /strategy` (also written to `hermes/generated/`).
     Risk limits are identical
     either way.
   - `AllowLive` → leave **false**; the strategy refuses to trade a live (brokerage)
     account unless this is explicitly true. Simulated **Sim*** and **Playback** accounts
     always trade with it off.
4. Select your account (**Sim101** or a **Playback** account) and enable the strategy.
   The strategy reports the selected account to the bridge automatically, so the
   dashboard, `/health`, and logs follow your choice — there's nothing to set in the
   bridge config (its `execution.account` is only a fallback until the strategy connects).

## What it does

| Event | Action |
| --- | --- |
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

## Dashboard — see what the agent is doing

The strategy draws the agent's **support/resistance lines** on the chart and a small,
draggable **"HERMES — DASHBOARD"** button (top-left by default). The button has a status
dot — **green** = bridge reachable, **amber** = connecting, **red** = offline — and it
polls only `GET /health` (for the dot) + `GET /levels.txt` (for the S/R lines).

**Click the button** to open the bridge's full HTML dashboard (position, P&L, trades,
daily-goal status, data age, the last decision + rationale, recent decisions, the armed
plan, and the authored playbook). It opens **inside a NinjaTrader window** using an
embedded **WebView2** (Chromium); **drag** the button to move it, **double-click** to snap
it back to the corner. Its position persists with the workspace.

Button knobs on the strategy: `RefreshSeconds`, `FontSize`, `ShowLevels`.

### Enabling the embedded window (WebView2)

The embedded window needs the WebView2 control. The strategy loads it **by reflection**, so
it **compiles and runs without it** — if WebView2 isn't available the button simply opens the
dashboard in your **default browser** instead. To get the in-NinjaTrader window:

1. In the **NinjaScript Editor**, right-click **References… → Add**, and add
   `Microsoft.Web.WebView2.Core.dll` and `Microsoft.Web.WebView2.Wpf.dll` (from the
   `Microsoft.Web.WebView2` NuGet package), then **Compile** (F5).
2. Ensure the **WebView2 Runtime** is installed (it ships with modern Edge; otherwise grab
   the Evergreen runtime from Microsoft). The first open creates a user-data folder under
   `%LOCALAPPDATA%\HermesDashboard\WebView2`.

> **Upgrading from the old on-chart card / separate `HermesDashboard` indicator?** Both are
> gone — the card was replaced by this button, and the indicator was folded into the strategy
> earlier. **Remove the `HermesDashboard` indicator from your charts** and delete it from
> `Documents/NinjaTrader 8/bin/Custom/Indicators/`.

Prefer a plain browser? The bridge serves the same **web dashboard** at
`http://<bridge-host>:8787/` (auto-refreshing) — open it on the Mac or inside the VM.
