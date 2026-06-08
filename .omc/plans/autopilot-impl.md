# Implementation Plan — Hermes × NinjaTrader 8

> Phase 1 output. Derived from `.omc/autopilot/spec.md`.

## Repository layout

```
hermes-trading-agent/
├── README.md                     # overview + quickstart + architecture diagram
├── Makefile                      # setup / test / lint / run / replay
├── .gitignore
├── config/
│   └── trading.yaml              # instrument, timeframe, strategy params, risk, daily goal
├── docs/
│   ├── ARCHITECTURE.md           # components, data flow, message contract, topologies
│   ├── SETUP.md                  # install Hermes, install NinjaScript, run bridge
│   └── SAFETY.md                 # risk gate, sim-first, kill switch, live checklist
├── ninjatrader/
│   └── HermesBridgeStrategy.cs   # NinjaScript Strategy (C#)
├── bridge/
│   ├── pyproject.toml            # package + deps (fastapi, uvicorn, pydantic, httpx, pyyaml)
│   ├── README.md
│   ├── hermes_bridge/
│   │   ├── __init__.py
│   │   ├── config.py             # load+validate trading.yaml; env overrides
│   │   ├── models.py             # Bar, BarBatch, Decision, OrderCommand, Fill, AccountState
│   │   ├── store.py              # bar buffer (deque + optional sqlite), indicators helpers
│   │   ├── indicators.py         # ema, atr, swing highs/lows, simple order-flow proxies
│   │   ├── session.py            # SessionState: P&L, trade count, position, daily goal/halt
│   │   ├── risk.py               # RiskGate: validate/clamp every command (hard limits)
│   │   ├── agent_client.py       # AgentClient ABC; MockAgentClient (rules); HermesAgentClient
│   │   ├── engine.py             # per-bar orchestration: ingest→analyze→risk→enqueue
│   │   ├── server.py             # FastAPI app + command queue + endpoints
│   │   └── cli.py                # `hermes-bridge serve` / `replay`
│   ├── replay/
│   │   └── sample_bars.csv       # synthetic bars for the replay harness/tests
│   └── tests/
│       ├── conftest.py
│       ├── test_indicators.py
│       ├── test_store.py
│       ├── test_risk.py
│       ├── test_session.py
│       ├── test_engine_mock.py   # full enter→manage→exit cycle on replay
│       └── test_server.py        # API contract via TestClient
├── hermes/
│   ├── README.md                 # how to install these into ~/.hermes / project
│   ├── context/
│   │   ├── HERMES.md             # project context: identity + operating loop
│   │   ├── strategy.md           # the specific way it trades
│   │   ├── order-flow.md         # knowledge base: order flow
│   │   ├── price-action.md       # knowledge base: price action
│   │   ├── risk-management.md    # risk rules (agent-facing)
│   │   └── daily-goal.md         # daily goal + session discipline
│   ├── personalities/
│   │   └── hermes-trader.md      # SOUL/personality
│   ├── tools/
│   │   └── ninjatrader.py        # registry.register nt_recent_bars / nt_account_status /
│   │                             # nt_place_order / nt_flatten / nt_session_status
│   └── cron/
│       └── trading-session.yaml  # optional: arm/disarm around RTH
└── scripts/
    ├── install_hermes.sh         # run official installer, copy our customization in
    ├── run_bridge.sh
    └── healthcheck.sh
```

## Message contract (NinjaTrader ⇄ Bridge), HTTP/JSON

- `POST /ingest/history` `{instrument, timeframe, bars:[Bar...]}` → `{ok, stored}`
- `POST /ingest/bar` `{instrument, timeframe, bar:Bar}` → `{ok, decision:Decision}`
  - bridge triggers analysis synchronously-ish; returns the decision for logging. Orders are
    NOT executed from this response — they go through the queue (single source of truth).
- `GET  /commands/next?strategy_id=...` → `{command:OrderCommand|null}` (NT polls)
- `POST /ingest/fill` `{order_id, side, qty, price, ts, position_after, realized_pnl}` → `{ok}`
- `POST /control/flatten` (kill switch) → `{ok}`
- `GET  /session/status` → SessionState
- `GET  /health` → `{ok, version}`
- Tool-facing (used by Hermes `nt_*` tools): `GET /bars/recent?n=`, `GET /account`,
  `POST /agent/command` (place/exit/flatten — same risk gate), `GET /session/status`.

`Bar = {ts, open, high, low, close, volume, is_closed, bid_vol?, ask_vol?}`
`OrderCommand = {id, action: ENTER_LONG|ENTER_SHORT|EXIT|FLATTEN, qty, stop_ticks|stop_price, target_ticks|target_price, reason}`
`Decision = {action: ENTER_LONG|ENTER_SHORT|EXIT|WAIT, confidence, qty, stop, target, rationale}`

## Risk gate (server-side, hard enforcement) — `risk.py`

Every command (whether from the engine, the LLM tool, or manual) is validated:
1. **Trading halted?** (daily goal hit / daily max loss hit / kill-switch) → reject entries,
   allow EXIT/FLATTEN only.
2. **Session window** (optional RTH guard) → outside → reject entries.
3. **Max trades/day** reached → reject entries.
4. **Position cap**: clamp/reject if `|position+qty| > max_contracts`.
5. **Protective stop mandatory**: every ENTER must have a stop; if missing, inject default
   `stop_ticks` from config. Compute per-trade $ risk = stop_ticks × tick_value × qty;
   reject if > `max_risk_per_trade`.
6. **Daily loss projection**: if worst-case (realized + open risk) would breach
   `max_daily_loss`, reject/clamp.
Returns `RiskDecision{approved, command(maybe-clamped), reasons[]}`. Pure + unit-tested.

## Session & daily goal — `session.py`

Tracks `realized_pnl`, `open_position`, `trades_today`, `day` (rolls at config session start).
- `daily_profit_target` hit → `halt(reason="goal")` → engine flattens, only EXIT allowed.
- `max_daily_loss` hit → `halt(reason="max_loss")` → flatten + halt.
- exposes `status()` for the agent tool + API.

## Decision engine — `engine.py`

`on_bar(bar)`:
1. store bar; update indicators/session marks.
2. compute deterministic **context features** (trend, swings, simple order-flow proxies).
3. if flat & not halted → ask agent `analyze(features, recent_bars, session)`; if in a
   position → ask agent `manage(...)`. (MockAgentClient returns rules-based decisions.)
4. translate Decision → OrderCommand, run through **RiskGate**, enqueue if approved.
5. return Decision (for the POST response/logging).

## AgentClient — `agent_client.py`

- `AgentClient` ABC: `analyze(ctx) -> Decision`, `manage(ctx) -> Decision`.
- `MockAgentClient`: deterministic order-flow/price-action rules (EMA trend + pullback +
  swing break + stop/target by ATR). Makes the whole system runnable & tested with no LLM.
- `HermesAgentClient`: builds a structured prompt from ctx + system message that points at
  the context files; calls Hermes `AIAgent.run_conversation(user, system)` (documented API),
  parses a fenced-JSON Decision from the reply. Import of Hermes is lazy and adapter-isolated
  so a version mismatch degrades to a clear error, and the system can fall back to Mock.

## NinjaScript Strategy — `HermesBridgeStrategy.cs`

- Properties: BridgeHost, BridgePort, StrategyId, MaxHttpMs, SendHistory(bool).
- `State.Configure`: `Calculate = Calculate.OnBarClose`; set account = Sim.
- `State.Realtime` transition: serialize all bars (0..CurrentBar) → `POST /ingest/history`.
- `OnBarUpdate` (realtime only): build Bar → `POST /ingest/bar`; then `GET /commands/next`;
  marshal execution to the strategy thread via `TriggerCustomEvent`.
- Execution: ENTER_LONG→`EnterLong(qty)` + `SetStopLoss/SetProfitTarget`; EXIT→`ExitLong/Short`;
  FLATTEN→exit all. `OnExecutionUpdate`/`OnPositionUpdate` → `POST /ingest/fill`.
- All HTTP off the UI thread (Task.Run), tolerant of bridge downtime (log + continue).
- Heavy inline comments since it can't be compiled here.

## Hermes customization

- **Context files** in real Hermes style (project markdown that shapes every conversation):
  identity + the strategy + knowledge + risk + daily goal. These are what make the agent
  "trade a specific way."
- **tools/ninjatrader.py**: `registry.register(name="nt_recent_bars"/"nt_place_order"/...,
  toolset="ninjatrader", schema=..., handler=...)` — thin httpx clients to the bridge. Place/
  exit go through the bridge risk gate, so the agent can never bypass risk.
- **personality** + optional **cron** to arm/disarm around the session.

## Build order

1. Bridge package (models → indicators → store → session → risk → agent_client → engine →
   server → cli) + tests.  ← do first; it's what I can actually run.
2. NinjaScript strategy.
3. Hermes context/tools/personality/cron.
4. config + scripts + docs + Makefile + git init.
5. QA: `uv venv` (3.11), install, `ruff`, `pytest` → green.
6. Validation: completeness + safety + quality review; fix; re-verify.

## Risks / mitigations

- *Can't compile NinjaScript here* → write to documented NT8 API, mark assumptions, provide a
  bridge-side replay harness that exercises the same message contract.
- *Hermes API drift* → isolate behind `HermesAgentClient` adapter + Mock fallback; document
  the two `AIAgent` entry points.
- *Auto-exec safety* → server-side risk gate is the single chokepoint; Sim default; kill
  switch; live behind explicit flag.
- *Cross-OS (Mac brain / Windows NT)* → HTTP over configurable host:port; document both
  topologies.
