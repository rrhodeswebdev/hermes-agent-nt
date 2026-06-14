# CONTEXT — `bridge/` (the Python safety bridge)

> Routed from the root [`CLAUDE.md`](../CLAUDE.md). Read that first for project-wide
> identity, rules, and naming. This file is the local map for the bridge workspace.

## What this workspace is

`hermes-bridge` is the always-on Python process that sits between NinjaTrader 8 and the
decision brain. **It is the single server-side safety authority** — the only thing that can
emit an order to NinjaTrader, and every order (engine-produced or manual) passes the
`RiskGate` first. It owns bar history, session/daily-goal state, the command queue, the
between-bars plan cycle, and the self-improvement reflection.

Package lives in `bridge/hermes_bridge/`; installed editable as the `hermes-bridge` CLI.

## Module map (`bridge/hermes_bridge/`)

| Module | Responsibility |
| --- | --- |
| `server.py` | FastAPI app + HTTP endpoints (the contract NinjaTrader calls). |
| `models.py` | Pydantic message contract: `Bar`, `Decision`, `OrderCommand`, `Fill`, `AccountState`. The C# side serializes against these. |
| `config.py` | `BridgeConfig` — loads `config/trading.yaml`, deep-merges `trading.local.yaml`, applies env overrides. |
| `cli.py` | `hermes-bridge` entrypoint: `serve`, `replay`. |
| `risk.py` | `RiskGate` — hard limits: position caps, per-trade $ risk, max trades/day, mandatory stop, daily-loss projection, halt/flatten, **major-news blackout**. |
| `news.py` | `NewsGuard` — fetches an economic calendar (`source: json` feed, or `forexfactory` direct-scrape); the `RiskGate` blocks entries within ±`window_minutes` of a high-impact event for the configured currencies (exits always allowed). Fails open (cached, then trade). Surfaced on `/health` + dashboard. |
| `engine.py` | `TradingEngine` — turns a `Decision` into an `OrderCommand`; enforces engine-side, brain-agnostic breakeven + trail. |
| `stops.py` | Vol-scaled, band-clamped stop sizing; breakeven/trail math. |
| `reauthor.py` | `ReauthorGovernor` — agent-mode decision for WHEN to re-author the playbook (trend-flip / uncovered-regime / vol-shock / ceiling / failed-author retry). Pure state machine; the engine owns the guards + the act. |
| `session.py` | `SessionState` — P&L, daily goal, halt/flatten state. |
| `store.py` | `BarStore` — in-memory bar history. |
| `indicators.py` | ATR, swings, delta, swing-**structure** regime classification (HH/HL vs LH/LL vs contained). |
| `levels.py` | Swing-pivot S/R zones (served at `GET /levels`, fed to the plan prompt). |
| `plan.py` | Pre-armed plan cycle — analysis runs *between* bars, arms close conditions; each bar close answers from the armed plan (LLM off the critical path). |
| `agent_client.py` | `AgentClient` protocol + `MockAgentClient` (the deterministic rules engine / safe fallback). |
| `claude_agent.py` | `ClaudeAgentClient` — gathers the live knowledge/learned/playbook pieces, runs the call, parses a schema-validated `Decision`. |
| `prompts.py` | Pure system-prompt assembly: composes the brain's prompt (framework knowledge + active playbook + learned memory + task instruction) from its parts. The single place "what the brain sees" is built. |
| `claude_cli.py` | Low-level `claude -p --safe-mode` invocation (oneshot + persistent session). |
| `reflect.py` | Post-trade self-improvement: proposes lesson/notes/profile updates into `hermes/learned/`. |
| `memory.py` | Loads learned memory (lessons, agent notes, profile, similar past trades) for the decision prompt. |
| `journal.py` | Episodic trade journal (`bridge/state/journal.jsonl`). |
| `dashboard.py` | Text + self-contained auto-refreshing HTML dashboard. |
| `replay_sim.py` | Offline replay simulator (full enter→manage→exit→daily-goal loop, no NT/LLM). |

## Where things live

- `tests/` — pytest suite. `replay/sample_bars.csv` — offline replay fixture.
- `state/` — runtime journal + state (**gitignored**, never commit).
- `scripts/` — bridge-local helpers. `pyproject.toml` — deps, ruff, pytest config.

## Working rules (local)

- **Run everything from the venv:** `bridge/.venv/bin/pytest` and
  `bridge/.venv/bin/hermes-bridge …`. Tests must pass and `ruff` must be clean before commit.
- **The `RiskGate` is invariant.** Never add an order path that bypasses it. Config in
  `trading.yaml` is the enforced source of truth — context prose only guides the brain.
- **Any brain failure degrades to `WAIT`;** open positions stay protected by the resting
  bracket. Preserve that fallback in any change to the decision path.
- Python 3.11+, ruff line-length 100, lint set `E,F,I,UP,B`. snake_case modules/functions,
  PascalCase classes. Tests are `test_*.py`.
- HTTP contract changes must stay in sync with `ninjatrader/HermesBridgeStrategy.cs`
  (see that workspace's [`CONTEXT.md`](../ninjatrader/CONTEXT.md)).
