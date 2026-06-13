# Setup

Three things to stand up: the **bridge**, the **decision brain** (the Claude CLI), and the
**NinjaScript** strategy. Validate the whole loop with the mock engine first (no LLM,
no NinjaTrader), then switch on the Claude brain, then connect NinjaTrader on a **Sim**
account.

## 0. Prerequisites

- `uv` (Python manager) — https://docs.astral.sh/uv/
- NinjaTrader 8 (Windows) for live/sim chart trading
- The **Claude Code CLI**, logged in on your Claude subscription (no API key) — optional
  until step 2. Install: https://claude.com/claude-code. Verify with `claude --version`.

## 1. Bridge — verify the loop with the mock engine

```bash
cd bridge
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"

.venv/bin/pytest                       # 60 tests should pass
# Full enter→manage→exit→daily-goal loop on synthetic bars, no LLM:
.venv/bin/hermes-bridge replay replay/sample_bars.csv -v --agent mock --config ../config/trading.yaml
```

Then run the server:

```bash
../scripts/run_bridge.sh               # serves on 0.0.0.0:8787
../scripts/healthcheck.sh              # GET /health + /session/status
```

Edit `config/trading.yaml` for your instrument, risk limits, and daily goal.

## 2. Claude brain — log in and switch it on

The brain is the `claude` CLI in headless print mode. It runs on your Claude
subscription (no `ANTHROPIC_API_KEY`) and is isolated from your global CLAUDE.md, hooks,
MCP, and skills via `--safe-mode`.

```bash
# Make sure the CLI is installed and logged in:
claude --version          # confirms it's on PATH
claude                    # if it prompts, run /login (Claude subscription) once, then exit
```

Switch the engine to the LLM in `config/trading.yaml`:

```yaml
agent:
  client: claude
  claude:
    model: haiku          # haiku = fastest decisions; sonnet/opus for more deliberation
    max_thinking_tokens: 0  # speed lever: 0 ≈ 10s/decision; raise (e.g. 1024) for more reasoning
    context_dir: /absolute/path/to/hermes/context
```

The trading knowledge in `hermes/context/*.md` is loaded verbatim into Claude's system
prompt, so the agent trades the configured way. Restart the bridge after any config or
context change. If Claude errors, times out, or replies unparseably, the bridge safely
falls back to `WAIT` (no trade) — open positions stay protected by the resting bracket in
NinjaTrader.

> Latency note: extended "thinking" tokens dominate decision time. `max_thinking_tokens: 0`
> keeps a decision around ~10s; leaving it uncapped can run 30–50s (and risk hitting
> `timeout_s`). On a 1-minute chart, prefer `haiku` + a capped budget.

## 3. NinjaTrader — install the strategy (Sim)

See `ninjatrader/README.md`. Summary:

1. NinjaScript Editor → new strategy `HermesBridgeStrategy`, paste
   `ninjatrader/HermesBridgeStrategy.cs`, **Compile** (F5).
2. Open a chart (your instrument + timeframe). Right-click → **Strategies…** → add
   **HermesBridgeStrategy**.
3. Set `BridgeHost`/`BridgePort` (e.g. `127.0.0.1` / `8787`), `StrategyId`
   (= `strategy_id`), `SendHistory: true`, **`AllowLive: false`**.
4. Select your account — a **Sim** (e.g. `Sim101`) or **Playback** account — and enable the
   strategy. The selected account is auto-detected and reported to the bridge (shown on
   `/health` and the dashboard); the `execution.account` config value is only a fallback
   until the strategy connects.

On enable it bulk-uploads history, then streams each closed bar; approved orders appear
on the chart with their stop/target bracket.

## 4. Daily operation

- Start the bridge (`./start.sh` or `scripts/run_bridge.sh`), then enable the strategy on
  the chart.
- Watch `scripts/healthcheck.sh` / the bridge logs / the dashboard at `http://localhost:8787/`.
- Kill switch any time: `curl -X POST http://127.0.0.1:8787/control/flatten`
  (flattens + halts for the day). `POST /control/resume` clears the halt.

See `docs/SAFETY.md` before going anywhere near a live account.
