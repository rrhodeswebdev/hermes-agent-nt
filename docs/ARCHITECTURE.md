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
                                                  per closed   │  ▲ JSON decision
                                                  bar: spawn    ▼  │ (structured_output)
                                                ┌───────────────────────────┐
                                                │   claude -p  (Claude CLI)  │
                                                │   headless, --safe-mode,   │
                                                │   on your subscription     │
                                                │   • context files in the   │
                                                │     system prompt          │
                                                │   • returns ENTER/EXIT/WAIT │
                                                └───────────────────────────┘
```

Two long-running programs, plus a short-lived brain the bridge spawns each bar:

1. **NinjaTrader 8 + `HermesBridgeStrategy`** (C#, Windows) — the market interface and
   order executor. Streams bars, executes risk-approved commands on the Sim account,
   reports fills.
2. **`hermes-bridge`** (Python, FastAPI) — the connector and **safety authority**. Owns
   bar history, the risk gate, session/daily-goal state, and the command queue. This is
   the single point that can emit an order to NinjaTrader.
3. **`claude -p`** (the Claude Code CLI) — the reasoning brain. The bridge shells out to it
   once per closed bar in headless print mode, on your **Claude subscription** (no API
   key), isolated via `--safe-mode`. The trading knowledge lives in `hermes/context/*.md`
   and is loaded verbatim into Claude's system prompt; Claude returns a schema-constrained
   JSON `Decision`. It provides judgment; the bridge provides safety. (It is not a
   long-running process — there's nothing to keep running.)

## Decision loop (per closed bar)

1. NinjaTrader closes a bar → `POST /ingest/bar`.
2. The bridge stores it, builds market context (structure/regime, ATR, swings, delta), and
   asks the decision engine for an action:
   - `agent.client: mock` → deterministic order-flow/price-action rules (no LLM).
   - `agent.client: claude` → the bridge runs `claude -p … --json-schema …` with the
     market state on stdin and reads back a validated `Decision` from `structured_output`.
3. The resulting `Decision` (ENTER/EXIT/WAIT + stop/target) is converted to an
   `OrderCommand` and passed through the **RiskGate**.
4. If approved, it is enqueued. NinjaTrader polls `GET /commands/next` and executes it
   (with a resting stop/target bracket), via `TriggerCustomEvent` on the strategy thread.
5. Fills come back via `POST /ingest/fill`; `SessionState` updates P&L; if the daily
   goal or max-loss is hit, the bridge flattens and halts.

## Hybrid engine

- The **bridge** is the always-on rules + safety half (fast, deterministic, testable).
- **Claude** is the judgment half (context-driven reasoning).
- The `MockAgentClient` *is* the rules engine and the safe fallback: if the `claude` call
  errors, times out, or returns an unparseable answer, the decision degrades to `WAIT`, and
  any open position is still protected by the resting bracket in NinjaTrader.

## Message contract

Defined in `bridge/hermes_bridge/models.py` and summarized in `bridge/README.md`. The C#
strategy serializes against these shapes. Key types: `Bar`, `Decision`, `OrderCommand`,
`Fill`, `AccountState`.

## Deployment topologies

NinjaTrader 8 is Windows-only; the bridge + the Claude CLI run on macOS/Linux/Windows.
Transport to NinjaTrader is HTTP over a configurable `host:port`, so:

- **A — Two machines:** the bridge (and the Claude CLI) on your Mac/Linux box, NinjaTrader
  on a Windows box. Set `server.host: 0.0.0.0` on the bridge and point the strategy's
  `BridgeHost` at the bridge machine's LAN IP. (Keep them on a trusted LAN — see SAFETY.md.)
- **B — One Windows box:** NinjaTrader on Windows, the bridge + Claude CLI under WSL2. Use
  `127.0.0.1` / `localhost` for both. Simplest and recommended to start.

## Timeframe & cadence

The strategy uses `Calculate.OnBarClose`: exactly one decision per closed bar. Choose a
timeframe (5m default) that matches how often you want the agent reasoning — sub-minute
bars invoke `claude -p` very frequently. Decision latency is dominated by extended
"thinking" tokens; cap it with `agent.claude.max_thinking_tokens` (default `0` ≈ ~10s;
uncapped ≈ 30–50s and may hit `timeout_s` → `WAIT`). Prefer `model: haiku` for fast
per-bar decisions.
