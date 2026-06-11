# Hermes × NinjaTrader 8 Trading Agent

An automated futures trading agent that streams **NinjaTrader 8** chart data to a
**decision brain** — by default **Claude**, via the `claude` CLI on your subscription
(no API key, no per-token billing) — which reasons about the market using a **specific
trading style** (*trend-pullback with order-flow confirmation*), trades it on a
**simulated account** with hard **risk management** and a **daily goal**, and runs a
**self-improving loop** that learns from every closed trade.

> The brain is pluggable, selected in `config/trading.yaml` (`agent.client`):
> **`claude`** (default — your subscription, no API key), **`mock`** (deterministic
> rules, no LLM), or **`hermes`** (Nous Research's runtime — optional, not required to
> install or run). The trading knowledge, strategy, risk rules, and daily goal live in
> editable **context files**. A Python **bridge** sits between NinjaTrader and the brain
> and is the single, server-side **safety authority**.

```
NinjaTrader 8  ──bars──▶  hermes-bridge  ──asks──▶  Decision brain
(NinjaScript C#)          (Python, risk gate)        (claude | mock | hermes)
       ▲                         │   ▲                 + trading context files
       └──── approved orders ◀────┘   └──── every order re-checked by the risk gate
```

## What it does

- **History on start, then every closed bar.** The NinjaScript strategy bulk-uploads all
  loaded bars, then streams each newly closed bar.
- **Decides enter / wait / exit** each bar — via the LLM (Claude by default, or Hermes)
  or a deterministic rules engine (mock), selectable in config.
- **Auto-executes on the Sim account** with a resting stop/target bracket.
- **Knows its strategy, risk, and daily goal** — defined in editable context files and
  enforced server-side.

## Repository map

| Path | What |
|------|------|
| `bridge/` | Python bridge: ingest, risk gate, session/daily-goal, engine, server, tests |
| `ninjatrader/HermesBridgeStrategy.cs` | NinjaScript Strategy (streams bars, executes orders) |
| `hermes/context/` | the trading knowledge/strategy/risk/goal (context files; fed to whichever brain) |
| `hermes/learned/` | self-improving memory: trader profile, agent notes, distilled lessons |
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

`start.sh` reads `config/trading.yaml`, creates the bridge venv on first run, picks the
right serve path for the configured brain (`mock`, Hermes `cli`, or Hermes `in_process`),
waits until the bridge is healthy, prints exactly what to plug into NinjaTrader (host,
`BridgePort`, `StrategyId`), then streams the logs. **Ctrl-C** stops it cleanly. You start
the **NinjaTrader** side yourself (compile + enable `HermesBridgeStrategy` on a Sim chart).

```bash
./start.sh --mock          # force the deterministic brain (no LLM)
./start.sh --check-hermes  # also do a live `hermes -z` ping before serving
```

## Start everything (Windows side)

NinjaTrader 8 is Windows-only; the bridge is plain Python, so run both on one box and
talk over `127.0.0.1`. The decision brain is **Claude** via the `claude` CLI on your
subscription — no Anthropic API key, no per-token billing.

```powershell
.\start.ps1                 # bring up the bridge using config/trading.yaml
.\start.ps1 -CheckClaude    # also ping Claude once (one real call) before serving
.\start.ps1 -Mock           # deterministic mock brain (no LLM)
```

Requires [`uv`](https://docs.astral.sh/uv/) and Claude Code (`claude` on PATH). The
trading brain is selected by `agent.client: claude` in `config/trading.yaml`; the trading
knowledge stays in `hermes/context/*.md`. Decisions run ~25–35s on Sonnet with full
context, so 2–3m bars (or `model: haiku`) give more headroom than 1m.

**Self-improving loop:** every closed trade is journaled (`bridge/state/journal.jsonl`) and
triggers a background reflection that distils lessons into `hermes/learned/` (git-tracked, so
you can watch — and revert — what it learns). Decisions are fed your profile, the agent's
notes, active lessons, and the most similar past trades. `agent.prefilter: mock` cuts Claude
calls by screening entries with the rules first.

## Quick start (no LLM, no NinjaTrader)

```bash
make setup          # create the bridge venv + install
make test           # 91 tests
make replay         # full enter→manage→exit→daily-goal loop on synthetic bars
```

Then follow **`docs/SETUP.md`** to pick your brain (Claude / mock / hermes) and connect
NinjaTrader on Sim.

## Safety first

Sim-first by design. Every order passes a server-side **RiskGate**; the daily goal
auto-flattens and halts; a kill switch is one request away; live trading is gated behind
explicit flags. **Read `docs/SAFETY.md` before going near real money.** This is software,
not financial advice.

## Status

- ✅ Bridge: implemented, `ruff` clean, **91/91 tests pass**, replay loop verified.
- ✅ NinjaScript strategy: written to the NT8 API (compile inside NinjaTrader).
- ✅ Hermes customization: context files, `nt_*` tools, personality, cron.
- ▶️ Next: define *your* exact strategy in `hermes/context/strategy.md` and validate on
  Sim across many sessions.
