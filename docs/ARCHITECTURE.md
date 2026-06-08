# Architecture

## Components

```
┌─────────────────────────┐     HTTP/JSON      ┌───────────────────────────┐
│   NinjaTrader 8 (Win)    │  ───────────────▶  │   hermes-bridge (Python)  │
│  HermesBridgeStrategy.cs │   bars, fills      │   FastAPI server          │
│  • OnBarUpdate (close)   │                    │   • BarStore (history)    │
│  • EnterLong/Short       │  ◀───────────────  │   • RiskGate (hard limits)│
│  • SetStop/SetTarget     │   order commands   │   • SessionState (P&L,    │
│  • OnExecutionUpdate     │                    │     daily goal, halt)     │
└─────────────────────────┘                    │   • TradingEngine         │
                                                │   • CommandQueue          │
                                                └──────────────┬────────────┘
                                                   calls each  │  ▲ nt_* tools
                                                   closed bar   ▼  │ (HTTP)
                                                ┌───────────────────────────┐
                                                │   Hermes Agent (Python)   │
                                                │   • context files         │
                                                │     (strategy/order-flow/ │
                                                │      price-action/risk/   │
                                                │      daily-goal)          │
                                                │   • nt_* tools            │
                                                │   • AIAgent.run_conversation
                                                └───────────────────────────┘
```

Three processes:

1. **NinjaTrader 8 + `HermesBridgeStrategy`** (C#, Windows) — the market interface and
   order executor. Streams bars, executes risk-approved commands on the Sim account,
   reports fills.
2. **`hermes-bridge`** (Python, FastAPI) — the connector and **safety authority**. Owns
   bar history, the risk gate, session/daily-goal state, and the command queue. This is
   the single point that can emit an order to NinjaTrader.
3. **Hermes Agent** (Python) — the reasoning brain. Customized via context files and the
   `nt_*` tools. Provides judgment; the bridge provides safety.

## Decision loop (per closed bar)

1. NinjaTrader closes a bar → `POST /ingest/bar`.
2. The bridge stores it, builds market context (EMA/ATR/swings/delta), and asks the
   decision engine for an action:
   - `agent.client: mock` → deterministic order-flow/price-action rules (no LLM).
   - `agent.client: hermes` → Hermes `AIAgent.run_conversation(...)`, guided by the
     context files; the agent may also call `nt_*` tools to look closer.
3. The resulting `Decision` (ENTER/EXIT/WAIT + stop/target) is converted to an
   `OrderCommand` and passed through the **RiskGate**.
4. If approved, it is enqueued. NinjaTrader polls `GET /commands/next` and executes it
   (with a resting stop/target bracket), via `TriggerCustomEvent` on the strategy thread.
5. Fills come back via `POST /ingest/fill`; `SessionState` updates P&L; if the daily
   goal or max-loss is hit, the bridge flattens and halts.

## Hybrid engine

- The **bridge** is the always-on rules + safety half (fast, deterministic, testable).
- **Hermes** is the judgment half (context-driven reasoning).
- The `MockAgentClient` *is* the rules engine and the safe fallback: if Hermes is
  unreachable or returns an unparseable answer, the decision degrades to `WAIT`, and any
  open position is still protected by the resting bracket in NinjaTrader.

## Message contract

Defined in `bridge/hermes_bridge/models.py` and summarized in `bridge/README.md`. Both
the C# strategy and the `nt_*` tools serialize against these shapes. Key types: `Bar`,
`Decision`, `OrderCommand`, `Fill`, `AccountState`.

## Deployment topologies

NinjaTrader 8 is Windows-only; Hermes + the bridge run on macOS/Linux/WSL2. Transport is
HTTP over a configurable `host:port`, so:

- **A — Two machines:** Hermes + bridge on your Mac/Linux box, NinjaTrader on a Windows
  box. Set `server.host: 0.0.0.0` on the bridge and point the strategy's `BridgeHost` at
  the bridge machine's LAN IP. (Keep them on a trusted LAN — see SAFETY.md.)
- **B — One Windows box:** NinjaTrader on Windows, Hermes + bridge under WSL2. Use
  `127.0.0.1` / `localhost` for both. Simplest and recommended to start.

## Timeframe & cadence

The strategy uses `Calculate.OnBarClose`: exactly one decision per closed bar. Choose a
timeframe (5m default) that matches how often you want the agent reasoning — sub-minute
bars call the agent very frequently (cost/latency with the LLM client).
