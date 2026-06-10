# Safety

Automated trading can lose money quickly. This system is built **Sim-first** with the
bridge as a single, server-side safety authority. Read this before connecting anything
to real money.

## The single chokepoint

**Every** order — whether from the deterministic engine or a manual API call — passes
through `RiskGate.evaluate()` before it can be queued for NinjaTrader. The agent cannot
bypass it. The gate enforces:

| Rule | Behaviour |
|------|-----------|
| Halted / daily goal hit | entries rejected; only EXIT/FLATTEN allowed |
| Flat-only entries | no pyramiding, averaging down, or one-step flips |
| Max trades/day | entries rejected past the cap |
| Position cap | size clamped to `max_contracts` |
| Mandatory stop | every entry gets a protective stop (default injected if missing) |
| Per-trade risk | size clamped so `stop$ × contracts ≤ max_risk_per_trade`; rejected if one contract is already too risky |
| Daily-loss projection | a trade whose worst-case stop-out would breach `max_daily_loss` is rejected |
| Risk-reducing always allowed | EXIT/FLATTEN are never blocked, even when halted |

Tune the numbers in `config/trading.yaml`.

## Daily goal = automatic stop

When realized P&L reaches `profit_target` **or** falls to `-max_daily_loss`, the bridge
**flattens any open position and halts** new entries until the next trading day. This is
enforced in `SessionState` + `TradingEngine`, independent of what the agent "wants."

> Day boundary note: v1 rolls the trading day on **UTC midnight** (`SessionState`). If
> your session day differs (e.g. CME 17:00 ET), adjust `_DayKey.from_ts` to key on your
> session timezone before relying on overnight resets.

## Layers of protection (defence in depth)

1. **NinjaScript account guard (the real sim/live interlock)** — `HermesBridgeStrategy`
   refuses to trade an account that doesn't look like a simulation account unless
   `AllowLive` is explicitly `true`. Only NinjaTrader knows the account, so this is the
   layer that actually prevents live orders.
2. **Bridge `execution.allow_live` (advisory posture)** — defaults `false`; surfaced in
   the startup log and `GET /health`. The bridge can't see NinjaTrader's account, so
   this flags *intent and visibility*, not hard enforcement; it does not by itself stop
   live trading (layer 1 does).
3. **RiskGate** — the rules above, on every order.
4. **Resting bracket** — every entry places a real stop + target in NinjaTrader, so even
   if the bridge or agent goes away, the position is protected by the exchange-side stop.
5. **Fail-safe agent** — if Claude errors, times out, or returns garbage, the decision is
   `WAIT` (never a trade).
6. **Kill switch** — `POST /control/flatten` flattens + halts immediately.

## Going live (only after thorough Sim validation)

Do **not** flip to live casually. Before considering it:

- Run on **Sim** across many sessions and varied conditions; review every trade.
- Confirm the risk numbers in `config/trading.yaml` reflect real account tolerance.
- Understand that the mock/LLM strategy here is a **starting template**, not a proven
  edge. Validate profitability yourself.

To enable live (at your own risk): set `execution.allow_live: true` in the config **and**
`AllowLive: true` on the NinjaScript strategy, and select the live account in
NinjaTrader. Start with the smallest size and the tightest daily limits.

## Network exposure

The bridge serves plain HTTP with **no authentication**. Run it on `127.0.0.1`
(same-machine / WSL2) or a **trusted private LAN** only. Do not expose port 8787 to the
public internet. If you must cross an untrusted network, put it behind a VPN or an
authenticating reverse proxy.

## Not financial advice

This is software, not investment advice. Trading futures involves substantial risk of
loss. You are responsible for any orders this system places.
