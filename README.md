# Hermes × NinjaTrader 8 Trading Agent

An automated futures trading agent that streams **NinjaTrader 8** chart data to the
**Hermes Agent** (Nous Research's open-source agent runtime), which reasons about the
market using a **specific trading style** — *trend-pullback with order-flow
confirmation* — and trades it on a **simulated account**, with hard **risk management**
and a **daily goal**.

> Hermes is *customized, not rebuilt*: its trading knowledge, strategy, risk rules, and
> daily goal live in **context files**, and it acts through **`nt_*` tools**. A Python
> **bridge** sits between NinjaTrader and Hermes and is the single, server-side **safety
> authority**.

```
NinjaTrader 8  ──bars──▶  hermes-bridge  ──asks──▶  Hermes Agent (LLM)
(NinjaScript C#)          (Python, risk gate)        + trading context files
       ▲                         │   ▲                 + nt_* tools
       └──── approved orders ◀────┘   └──── nt_place_order (re-checked by risk gate)
```

## What it does

- **History on start, then every closed bar.** The NinjaScript strategy bulk-uploads all
  loaded bars, then streams each newly closed bar.
- **Decides enter / wait / exit** each bar — via the LLM (Hermes) or a deterministic
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
| `docs/` | `ARCHITECTURE.md`, `SETUP.md`, `SAFETY.md` |
| `scripts/` | install / run / healthcheck helpers |

## Quick start (no LLM, no NinjaTrader)

```bash
make setup          # create the bridge venv + install
make test           # 37 tests
make replay         # full enter→manage→exit→daily-goal loop on synthetic bars
```

Then follow **`docs/SETUP.md`** to wire in Hermes and connect NinjaTrader on Sim.

## Safety first

Sim-first by design. Every order passes a server-side **RiskGate**; the daily goal
auto-flattens and halts; a kill switch is one request away; live trading is gated behind
explicit flags. **Read `docs/SAFETY.md` before going near real money.** This is software,
not financial advice.

## Status

- ✅ Bridge: implemented, `ruff` clean, **37/37 tests pass**, replay loop verified.
- ✅ NinjaScript strategy: written to the NT8 API (compile inside NinjaTrader).
- ✅ Hermes customization: context files, `nt_*` tools, personality, cron.
- ▶️ Next: define *your* exact strategy in `hermes/context/strategy.md` and validate on
  Sim across many sessions.
