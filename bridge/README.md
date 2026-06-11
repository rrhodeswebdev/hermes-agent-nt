# hermes-bridge

The Python connector between **NinjaTrader 8** (NinjaScript Strategy, C#) and the
**Hermes Agent** trading brain. It ingests bars, enforces a server-side **risk
gate**, tracks the trading session + **daily goal**, and queues risk-approved order
commands for NinjaTrader to execute on the **Sim** account.

```
NinjaTrader 8 ──HTTP──▶ hermes-bridge ──▶ Hermes Agent (LLM)
   (C# Strategy)         (this package)      + nt_* tools ──┐
                            ▲   risk gate / session / queue ◀┘
                            └────── commands ──▶ NinjaTrader
```

## Quick start

```bash
# from the bridge/ directory
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"

# run the tests + replay demo (no LLM, no NinjaTrader needed)
.venv/bin/pytest
.venv/bin/hermes-bridge replay replay/sample_bars.csv -v --config ../config/trading.yaml

# serve the bridge for NinjaTrader to connect to
.venv/bin/hermes-bridge serve --config ../config/trading.yaml
```

## Decision engine

The bridge is the "rules + safety" half of a **hybrid** engine:

- `MockAgentClient` — deterministic order-flow + price-action rules. Runs the whole
  loop with **no LLM**, and is the safe fallback.
- `HermesAgentClient` — delegates judgment to the installed Hermes runtime
  (`AIAgent.run_conversation`) using the trading-knowledge **context files**, and
  parses a JSON `Decision` back. Any failure degrades to `WAIT`.
- `ClaudeAgentClient` — delegates judgment to Claude Code in headless print mode
  (`claude -p --safe-mode`) on your subscription (no metered API), isolated from your
  global CLAUDE.md/hooks/MCP. Uses the same context files and `--json-schema` for a
  validated `Decision`; any failure degrades to `WAIT`.
- **Self-improvement** (`reflect.py`) — after each closed trade a background, tool-less
  Claude call proposes lesson/notes/profile updates (schema-validated); the bridge applies
  them to `hermes/learned/`. `agent.prefilter: mock` screens entries with the deterministic
  rules so Claude is only spent on candidate setups. `POST /control/curate` consolidates lessons.

Select with `agent.client: mock | hermes | claude` in `config/trading.yaml`.

## Safety model

Every order — from the engine, the Hermes `nt_place_order` tool, or a manual API
call — passes through `RiskGate` before it can be queued. The gate enforces position
caps, per-trade dollar risk, max trades/day, a mandatory protective stop on every
entry, the daily-loss projection, and the halt/flatten on the daily goal. See
`../docs/SAFETY.md`.

## HTTP contract

| Method & path | Caller | Purpose |
|---|---|---|
| `POST /ingest/history` | NinjaTrader | bulk-load all historical bars on start |
| `POST /ingest/bar` | NinjaTrader | one newly-closed bar → returns the `Decision` |
| `GET /commands/next?strategy_id=` | NinjaTrader | poll the next risk-approved order |
| `POST /ingest/fill` | NinjaTrader | report a fill (updates P&L / position) |
| `GET /bars/recent?n=` | Hermes tool | recent bars for the agent to review |
| `GET /account` · `GET /session/status` | Hermes tool / ops | account + session state |
| `POST /agent/command` | Hermes tool | agent places/exits an order (risk-gated) |
| `POST /control/flatten` | ops | kill switch: flatten + halt |
| `POST /control/resume` | ops | clear a halt |
| `GET /health` | ops | liveness |

See `hermes_bridge/models.py` for the exact JSON shapes.
