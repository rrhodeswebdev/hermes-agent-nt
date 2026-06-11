# Setup

Three things to stand up: the **bridge**, the **Hermes** customization, and the
**NinjaScript** strategy. Validate the whole loop with the mock engine first (no LLM,
no NinjaTrader), then wire in Hermes, then connect NinjaTrader on a **Sim** account.

## 0. Prerequisites

- `uv` (Python manager) — https://docs.astral.sh/uv/
- NinjaTrader 8 (Windows) for live/sim chart trading
- A Hermes Agent install (macOS/Linux/WSL2) — optional until step 3

## 1. Bridge — verify the loop with the mock engine

```bash
cd bridge
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"

.venv/bin/pytest                       # 37 tests should pass
# Full enter→manage→exit→daily-goal loop on synthetic bars, no LLM:
.venv/bin/hermes-bridge replay replay/sample_bars.csv -v --config ../config/trading.yaml
```

Then run the server:

```bash
../scripts/run_bridge.sh               # serves on 0.0.0.0:8787
../scripts/healthcheck.sh              # GET /health + /session/status
```

Edit `config/trading.yaml` for your instrument, risk limits, and daily goal.

## 2. Hermes — install and customize

```bash
# Install the Hermes runtime (review it first), then copy our customization:
scripts/install_hermes.sh --install-hermes
# or, if Hermes is already installed, just copy customization:
scripts/install_hermes.sh
```

Then (see `hermes/README.md` for detail):

- Point your Hermes **project context** at `hermes/context/*.md`.
- Enable the **`ninjatrader`** toolset for the agent.
- Set env: `HERMES_BRIDGE_URL=http://127.0.0.1:8787`,
  `HERMES_STRATEGY_ID=hermes-default` (match `config/trading.yaml`).

Switch the engine to the LLM when ready:

```yaml
# config/trading.yaml
agent:
  client: hermes
```

Restart the bridge. If Hermes is unreachable or replies unparseably, the bridge safely
falls back to `WAIT` (no trade).

## 3. NinjaTrader — install the strategy (Sim)

See `ninjatrader/README.md`. Summary:

1. NinjaScript Editor → new strategy `HermesBridgeStrategy`, paste
   `ninjatrader/HermesBridgeStrategy.cs`, **Compile** (F5).
2. Open a chart (your instrument + timeframe). Right-click → **Strategies…** → add
   **HermesBridgeStrategy**.
3. Set `BridgeHost`/`BridgePort` (e.g. `127.0.0.1` / `8787`), `StrategyId`
   (= `strategy_id`), `SendHistory: true`, **`AllowLive: false`**.
4. Select the **Sim101** account and enable the strategy.

On enable it bulk-uploads history, then streams each closed bar; approved orders appear
on the chart with their stop/target bracket.

## 4. Daily operation

- Start the bridge, then enable the strategy on the chart.
- Watch `scripts/healthcheck.sh` / the bridge logs.
- Kill switch any time: `curl -X POST $HERMES_BRIDGE_URL/control/flatten`
  (flattens + halts for the day). `POST /control/resume` clears the halt.

See `docs/SAFETY.md` before going anywhere near a live account.
