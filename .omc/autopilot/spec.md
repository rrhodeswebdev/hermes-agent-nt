# Hermes × NinjaTrader 8 Trading Agent — Specification

> Phase 0 (Expansion) output. Source: autopilot request + clarifying answers (2026-06-07).

## 1. Vision

A trading agent that reasons about live market data and trades a **specific, learnable
style** (order-flow + price-action) on **NinjaTrader 8**, on a **Simulated (paper) account
first**. The agent's "brain" is **Hermes Agent** (Nous Research's open-source autonomous
agent runtime), customized — not rebuilt — through its native extension points:

- **Context files** carry the trading knowledge, the specific strategy, risk rules, and
  the daily goal (markdown injected into every conversation).
- **Custom tools** (`tools/*.py`, `registry.register(...)`) give Hermes the ability to read
  recent bars and place/exit trades.
- The agent decides, on each closed bar, whether to **enter / wait / exit**.

NinjaTrader 8 is C#/.NET; the connection to it is a **NinjaScript Strategy** (chosen over an
AddOn — see §4). A small **Python bridge** sits between NinjaTrader and Hermes.

## 2. Actors & Components

| Component | Tech | Responsibility |
|-----------|------|----------------|
| **HermesBridgeStrategy** | NinjaScript (C#, .NET Fx 4.8) | Streams historical bars on load + each new closed bar; executes trade commands on the Sim account; reports fills/position. |
| **Bridge server** | Python 3.11 (FastAPI/uvicorn) | Ingests bars, stores history, triggers analysis per bar, enforces the **risk gate**, queues order commands for NinjaTrader, tracks session P&L and the daily goal. |
| **Hermes Agent** | Python (installed via official curl) | The reasoning brain. Customized with context files (knowledge/strategy/risk/goal), a trader personality, and `nt_*` tools that call the bridge. |
| **Config** | YAML | Instrument, timeframe, strategy params, risk limits, daily goal. |

## 3. Decision model (per clarifying answers)

- **Style:** order-flow + price-action. The agent *learns how to trade* from context files
  (knowledge docs), not from hardcoded signals.
- **Engine:** Hybrid. The **bridge** holds a deterministic pre-filter + hard guardrails
  (always on, fast, testable). **Hermes (LLM)** provides the judgment/reasoning using its
  knowledge context. A **rules-based MockAgentClient** implements the same decision interface
  so the *entire loop runs and is tested without an LLM or API key*, and serves as a
  fallback when Hermes is unavailable.
- **Cadence:** one analysis per **closed bar** (`Calculate.OnBarClose`). Timeframe is config
  (default 5-minute) to keep LLM cost/latency sane.

## 4. NinjaTrader integration decision: Strategy (not AddOn)

A **NinjaScript Strategy** is the correct vehicle:

- `OnBarUpdate()` gives a per-bar callback; `State.Historical` vs `State.Realtime` cleanly
  separates the history backfill from live bars.
- Native order/risk methods: `EnterLong/EnterShort/ExitLong/ExitShort`, `SetStopLoss`,
  `SetProfitTarget`, `Position.MarketPosition`, account selection (Sim101).
- All historical bars are available on the series at load → bulk backfill is trivial.

An AddOn is for app-level UI/infrastructure and cannot manage strategy orders cleanly. (A
thin AddOn that owns a persistent socket is a possible *later* enhancement; v1 is a
self-contained Strategy.)

## 5. Data flow

```
NinjaTrader 8 (Windows)            Bridge (Python)                 Hermes Agent (Python)
─────────────────────────          ───────────────────            ─────────────────────
OnStateChange→Realtime
  POST /ingest/history  ─────────▶ store all bars
OnBarUpdate (bar close)
  POST /ingest/bar      ─────────▶ append bar
                                    risk gate / session state
                                    trigger analysis ───────────▶ AIAgent.run_conversation
                                                                    reads context files
                                                                    calls nt_recent_bars ─┐
                                    serve tool HTTP ◀───────────────────────────────────┘
                                                                    calls nt_place_order ─┐
                                    RISK GATE validates ◀───────────────────────────────┘
                                    enqueue OrderCommand
  GET /commands/next    ◀───────── dequeue (risk-approved)
  execute on Sim (TriggerCustomEvent → EnterLong/Exit/SetStop/SetTarget)
  POST /ingest/fill     ─────────▶ update position + realized P&L
                                    daily-goal check → may flatten+halt
```

## 6. Functional requirements

- **FR1** Stream **all** historical bars to the agent side on strategy start.
- **FR2** Stream **each newly closed bar** in real time.
- **FR3** Agent analyzes and emits one of: `ENTER_LONG`, `ENTER_SHORT`, `EXIT`, `WAIT`
  (with size, stop, target, and rationale).
- **FR4** Auto-execute approved decisions on the **NinjaTrader Sim account**.
- **FR5** Agent understands and follows a **specific strategy** (order-flow + price-action),
  expressed in editable context files.
- **FR6** **Risk management** enforced server-side (hard limits) AND understood by the agent
  (soft guidance): per-trade risk, max position, max trades/day, max daily loss, stop on
  every entry.
- **FR7** **Daily goal**: a profit target and a max-loss for the session. On hit → flatten
  and halt new entries for the day.
- **FR8** Fully runnable/testable **without** an LLM (MockAgentClient) and **without**
  NinjaTrader (the bridge + a replay/simulator script).

## 7. Non-functional requirements

- **Safety-first:** Sim account only by default; the bridge is the single point that can
  emit orders, and **every** order passes the risk gate. A global kill-switch flattens and
  halts. Live trading is gated behind an explicit config flag + acknowledgement.
- **Decoupled:** transport is HTTP over a configurable `host:port` so the two topologies
  work: (a) Hermes+bridge on macOS/Linux, NinjaTrader on Windows over LAN; (b) everything on
  one Windows box with Hermes under WSL2 over localhost.
- **Testable:** the Python bridge has unit + API tests that run in CI/locally via `uv`.
- **Honest boundaries:** NinjaScript can only compile inside NinjaTrader; the Hermes
  programmatic entry points are wired to the documented `AIAgent` API with a clearly marked
  adapter the user confirms against their installed version.

## 8. Out of scope (v1)

- Live-money trading (built but flag-gated and documented as "validate on Sim first").
- Backtesting framework beyond a bar-replay harness for the bridge.
- Multi-instrument portfolio management (single instrument per strategy instance).
- A web dashboard (Hermes ships its own; not required for the loop).

## 9. Acceptance criteria

- [ ] Bridge starts, accepts history + bar ingest, and returns risk-approved commands.
- [ ] Risk gate blocks: oversize, over-trade-count, daily-loss breach, post-goal entries; and
      forces a protective stop on every entry.
- [ ] Daily goal hit → flatten + halt verified by test.
- [ ] MockAgentClient drives a full enter→manage→exit cycle on replayed bars (test).
- [ ] NinjaScript strategy compiles against NT8 API surface (manual, documented) and follows
      the documented message contract.
- [ ] Hermes context files + `nt_*` tools follow real Hermes conventions (`registry.register`).
- [ ] `make test` is green; `make lint` is clean.
