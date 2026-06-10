# Hermes × NinjaTrader 8 Trading Agent

An automated futures trading agent that streams **NinjaTrader 8** chart data to the
**Claude CLI** (Claude Code in headless print mode, on your subscription — no API key),
which reasons about the market using a **specific trading style** — *trend-pullback with
order-flow confirmation* — and trades it on a **simulated account**, with hard **risk
management** and a **daily goal**.

> The brain is *configured, not coded*: its trading knowledge, strategy, risk rules, and
> daily goal live in **context files** (`hermes/context/`) that are loaded verbatim into
> the system prompt. A Python **bridge** sits between NinjaTrader and the brain and is the
> single, server-side **safety authority** that actually places every order.

```
NinjaTrader 8  ──bars──▶  hermes-bridge  ──asks──▶  Claude CLI (LLM)
(NinjaScript C#)          (Python, risk gate)        + trading context files
       ▲                         │
       └──── approved orders ◀────┘   (every order re-checked by the risk gate)
```

## What it does

- **History on start, then every closed bar.** The NinjaScript strategy bulk-uploads all
  loaded bars, then streams each newly closed bar.
- **Decides enter / wait / exit** each bar — via the LLM (Claude) or a deterministic
  rules engine (mock), selectable in config.
- **Auto-executes on the Sim account** with a resting stop/target bracket.
- **Knows its strategy, risk, and daily goal** — defined in editable context files and
  enforced server-side.

## Repository map

| Path | What |
|------|------|
| `bridge/` | Python bridge: ingest, risk gate, session/daily-goal, engine, server, tests |
| `ninjatrader/HermesBridgeStrategy.cs` | NinjaScript Strategy (streams bars, executes orders) |
| `hermes/context/` | the trading knowledge/strategy/risk/goal (Hermes context files) |
| `hermes/tools/ninjatrader.py` | the agent's `nt_*` tools (registry.register) |
| `hermes/personalities/` · `hermes/cron/` | trader personality, optional session cron |
| `config/trading.yaml` | instrument, strategy params, risk limits, daily goal |
| `docs/` | `EASY-SETUP.md` (plain-English walkthrough), `ARCHITECTURE.md`, `SETUP.md`, `SAFETY.md` |
| `start.sh` | **one-command startup** for the whole Mac side (bridge + dashboard) |
| `scripts/` | install / run / healthcheck helpers (called by `start.sh` or standalone) |

> 🆕 **New to this? Start with [`docs/EASY-SETUP.md`](docs/EASY-SETUP.md)** — a simple,
> step-by-step setup guide written in plain language.

## Start everything (Mac side)

```bash
./start.sh          # the single command: brings up the whole Mac side
```

`start.sh` reads `config/trading.yaml`, creates the bridge venv on first run, validates the
configured brain (`mock` or `claude`), waits until the bridge is healthy, prints exactly
what to plug into NinjaTrader (host, `BridgePort`, `StrategyId`), then streams the logs.
**Ctrl-C** stops it cleanly. You start the **NinjaTrader** side yourself (compile + enable
`HermesBridgeStrategy` on a Sim chart).

```bash
./start.sh --mock          # force the deterministic brain (no LLM)
./start.sh --check-claude  # also do a live `claude -p` ping before serving
```

## Quick start (no LLM, no NinjaTrader)

```bash
make setup          # create the bridge venv + install
make test           # 57 tests
make replay         # offline mock replay: full enter→manage→exit→daily-goal loop
```

> ℹ️ `make replay` forces the deterministic **mock** brain so the demo stays offline. To
> replay *through Claude* (one live model call per bar):
> `hermes-bridge replay replay/sample_bars.csv --agent claude --config ../config/trading.yaml`

Then follow **`docs/SETUP.md`** to install the Claude CLI and connect NinjaTrader on Sim.

## Safety first

Sim-first by design. Every order passes a server-side **RiskGate**; the daily goal
auto-flattens and halts; a kill switch is one request away; live trading is gated behind
explicit flags. **Read `docs/SAFETY.md` before going near real money.** This is software,
not financial advice.

## Status

- ✅ Bridge: implemented, `ruff` clean, **57/57 tests pass**, replay loop verified.
- ✅ NinjaScript strategy: written to the NT8 API (compile inside NinjaTrader).
- ✅ Decision brain: the `claude` CLI on your subscription, guided by the
  `hermes/context/` files (loaded verbatim into the system prompt).
- ▶️ Next: define *your* exact strategy in `hermes/context/strategy.md` and validate on
  Sim across many sessions.
