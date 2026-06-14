# CLAUDE.md

Project guidance for Claude Code. Read this first, then follow **Routing** to the
`CONTEXT.md` of whichever workspace you're working in.

---

## Project identity

**Hermes × NinjaTrader 8 Trading Agent** — an automated futures **day-trading** agent for
NinjaTrader 8.

A NinjaScript strategy streams chart data to a decision brain (the **Claude CLI** on the
user's subscription — no API key — or a deterministic **mock** rules engine). The brain
reasons about the market using **order flow + price action** and returns an ENTER/EXIT/WAIT
`Decision`. A Python **bridge** sits in the middle as the single **safety authority**: it
risk-gates and places every order, trades a **simulated** account by default, enforces a
**daily goal**, and runs a **self-improving loop** that learns from every closed trade.

Core principles:

- **Configured, not coded** — trading knowledge, risk rules, and the daily goal live in
  plain-English context files, not in code.
- **Safety is server-side** — the bridge's `RiskGate` is the one authority that emits orders;
  it is identical across both strategy modes and never bypassed.
- **Sim-first** — live trading is gated behind explicit flags on both sides.
- **Hybrid brain** — Claude provides judgment; the bridge provides deterministic rules + the
  safe fallback (any brain failure degrades to `WAIT`).

```text
NinjaTrader 8  ──bars──▶  hermes-bridge  ──asks──▶  Decision brain (claude | mock)
(NinjaScript C#)          (Python, risk gate)        + trading context files
       ▲                         │   ▲                 returns ENTER / EXIT / WAIT
       └──── approved orders ◀────┘   └──── every order re-checked by the RiskGate
```

---

## Folder structure

```text
hermes-trading-agent/
├── CLAUDE.md                 # this file
├── README.md                 # public overview
├── Makefile                  # setup / test / replay shortcuts
├── start.sh / start.ps1      # one-command startup (Mac / Windows)
├── config/                   # trading configuration (the enforced source of truth)
│   ├── trading.yaml          #   shared template — committed, neutral defaults
│   └── trading.local.yaml    #   personal overrides — gitignored
├── bridge/                   # ★ the Python safety bridge  → bridge/CONTEXT.md
│   ├── hermes_bridge/        #   the package (server, risk, engine, brain clients, …)
│   ├── tests/                #   pytest suite
│   ├── replay/               #   offline replay fixtures
│   ├── state/                #   runtime journal/state (gitignored)
│   └── pyproject.toml        #   deps, ruff, pytest
├── hermes/                   # ★ trading knowledge & learned memory  → hermes/CONTEXT.md
│   ├── context/              #   the framework (committed, plain-English)
│   ├── generated/            #   agent-authored playbooks (gitignored, runtime)
│   ├── learned/              #   self-improving memory (live names gitignored)
│   └── personalities|tools|cron/  # legacy, unused by the Claude brain
├── ninjatrader/              # ★ the NinjaScript strategy (C#)  → ninjatrader/CONTEXT.md
│   └── HermesBridgeStrategy.cs
├── docs/                     # ARCHITECTURE, SETUP, EASY-SETUP, SAFETY, WALKTHROUGH
└── scripts/                  # install / run / healthcheck helpers
```

★ = a **workspace** with its own `CONTEXT.md` (see Routing).

---

## Project rules

1. **Never bypass the `RiskGate`.** Every order — engine or manual — passes
   `bridge/hermes_bridge/risk.py` before it can be queued. Do not add an order path around it.
2. **`config/trading.yaml` is the enforced truth.** The numbers there (risk, daily goal, sizing)
   are what the bridge enforces. The prose in `hermes/context/*.md` only *guides* the brain.
3. **Sim-first.** `execution.allow_live` and the strategy's `AllowLive` must both be explicitly
   true for real money. Never flip these as part of routine work. Read `docs/SAFETY.md`.
4. **Personal values stay local.** Put your account name and real risk tolerance in
   `config/trading.local.yaml` (gitignored, deep-merged on top of `trading.yaml`). Keep the
   committed template neutral.
5. **Brain failures degrade to `WAIT`.** Preserve this fallback; open positions stay protected
   by the resting bracket in NinjaTrader.
6. **The Claude brain does not call tools.** It reasons and returns a `Decision`; the bridge
   executes. Don't reintroduce tool-calling into the decision path.
7. **Don't commit runtime artifacts.** `hermes/generated/`, the live `hermes/learned/` names,
   and `bridge/state/` are gitignored per-checkout state. Commit `context/` edits and
   `*.example.md` templates only.
8. **Bridge changes must be green before commit:** run `bridge/.venv/bin/pytest` and keep
   `ruff` clean (line-length 100, lint set `E,F,I,UP,B`).
9. **Keep the contract in sync.** The HTTP/JSON shapes in `bridge/hermes_bridge/models.py` are
   what `ninjatrader/HermesBridgeStrategy.cs` serializes against — change them together.

---

## Naming conventions

| Domain | Convention | Examples |
| --- | --- | --- |
| Python modules / functions | `snake_case` | `agent_client.py`, `claude_cli.py` |
| Python classes | `PascalCase` | `BridgeConfig`, `RiskGate`, `TradingEngine`, `BarStore`, `SessionState` |
| Python tests | `test_*.py` under `bridge/tests/` | `test_risk.py` |
| Config keys (YAML) | `snake_case` | `max_risk_per_trade`, `daily_goal.profit_target` |
| C# strategy + params | `PascalCase` | `HermesBridgeStrategy`, `BridgeHost`, `StrategyId`, `UseAgentStrategies`, `AllowLive` |
| Context / docs files | `kebab-case.md` (docs in `UPPER-CASE.md`) | `order-flow.md`, `daily-goal.md`, `SAFETY.md` |
| Agent-authored playbooks | `SYMBOL-YYYYMMDD-HHMM.md` + `latest.md` | `MNQ-20260519-1314.md` |
| Learned templates vs live | `*.example.md` committed → copy to live name | `agent-notes.example.md` → `agent-notes.md` |
| CLI entrypoint | `hermes-bridge <command>` | `hermes-bridge serve`, `hermes-bridge replay` |

---

## Workspaces

The app is split into three primary domains plus supporting areas. Each primary workspace has
its own `CONTEXT.md` with a local map.

| Workspace | Path | Domain — what it owns |
| --- | --- | --- |
| **Bridge** (Python) | `bridge/` | The always-on connector and **single safety authority**. Bar ingest, `RiskGate`, session/daily-goal, command queue, between-bars plan cycle, decision engine (mock + Claude clients), self-improvement reflection, FastAPI server, dashboard, offline replay. The bulk of the executable code. |
| **Knowledge & memory** | `hermes/` | The brain's *configuration* — plain-English strategy/order-flow/price-action/risk/daily-goal context files (`context/`), agent-authored playbooks (`generated/`), and self-improving memory (`learned/`). No executable trading logic. |
| **NinjaTrader strategy** (C#) | `ninjatrader/` | The Windows market interface + order executor. Streams bars, executes risk-approved bracketed orders, reports fills, hosts the on-chart dashboard button. Compiles inside NinjaTrader, not part of the Python build. |

Supporting areas (no `CONTEXT.md`; covered here):

- **Configuration** — `config/`: `trading.yaml` (committed template) + `trading.local.yaml`
  (personal, gitignored). The enforced source of truth for all risk/sizing/goal numbers.
- **Docs** — `docs/`: `EASY-SETUP.md`, `SETUP.md`, `ARCHITECTURE.md`, `SAFETY.md`, `WALKTHROUGH.md`.
- **Ops / startup** — `start.sh`, `start.ps1`, `scripts/`: bring up the Mac/Windows side,
  health checks, run helpers.

---

## Routing

When working inside a workspace, **read its `CONTEXT.md` first** — it holds the local module
map, sub-area layout, and workspace-specific rules. Route by where the change lives:

| If you're working on… | Go to |
| --- | --- |
| Bridge logic, risk gate, engine, server, decision clients, tests, replay | [`bridge/CONTEXT.md`](bridge/CONTEXT.md) |
| Trading knowledge, context files, strategies, generated/learned memory | [`hermes/CONTEXT.md`](hermes/CONTEXT.md) |
| The NinjaScript strategy, order execution, on-chart dashboard | [`ninjatrader/CONTEXT.md`](ninjatrader/CONTEXT.md) |
| Config values (risk, sizing, daily goal, instrument) | `config/trading.yaml` (+ local override) — no CONTEXT.md |
| Setup, architecture, safety background | `docs/` — start with `docs/ARCHITECTURE.md` / `docs/SAFETY.md` |

**Convention:** each primary workspace owns a `CONTEXT.md` at its root. When you add a new
top-level domain, give it a `CONTEXT.md` and add a row to both the Workspaces and Routing
tables above. Keep each `CONTEXT.md` local to its workspace; keep cross-cutting rules here.
