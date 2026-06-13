# Trading knowledge & context

The decision brain is the **Claude CLI** (`claude -p`), and the files in **`context/`**
are its instructions: they're loaded *verbatim* into Claude's system prompt on every
decision. This is how the agent learns the strategy, order-flow read, price-action
context, risk rules, and daily goal — **in plain English, no code**. Edit the notes,
restart the bridge, and the agent trades the new way.

| File | What it teaches the brain |
|------|---------------------------|
| `context/HERMES.md` | the operating loop and non-negotiables (always use a stop, when unsure WAIT, one position at a time) |
| `context/strategy.md` | how the **active playbook** is selected (agent-authored vs your custom files) and the hard rules every setup obeys (bracket everything, never widen a stop, never flip in one step) |
| `context/order-flow.md` | reading buying vs. selling pressure (delta, absorption, exhaustion) — the confirmation layer |
| `context/price-action.md` | trend, structure, and location — the context layer |
| `context/risk-management.md` | the hard limits and behavioral rules (no revenge trading, no chasing, think in R) |
| `context/daily-goal.md` | trade to a daily plan; bank green days; walk away at the max loss |

These files are the **framework** — loaded in both strategy modes. The **playbook** (the
actual setups) is swappable; see below.

## Strategy source: agent-authored vs your own

The setups the brain trades come from the **active playbook**, chosen by NinjaTrader's
`UseAgentStrategies` toggle (default **on**) / the `strategies.source` config default:

- **Agent (`UseAgentStrategies` on / `source: agent`)** — at session start the brain
  studies the chart's historical bars and **authors its own playbook**, then trades it for
  the session. Nothing to pre-write. Each authored playbook is saved to `../hermes/generated/`
  (gitignored) and served at `GET /strategy` so you can see exactly what it invented.
- **Custom (`UseAgentStrategies` off / `source: custom`)** — the brain trades **your** own
  playbooks, dropped as `*.md` into `context/strategies/trending/` and
  `context/strategies/ranging/`. These dirs ship **empty** (just a `.gitkeep`); an empty
  set means the agent has no setup and simply WAITs. The agent invents nothing in this mode.

Either way the bridge's `RiskGate`, the protective brackets, and the Sim-account guard are
enforced identically — the toggle only changes where the *guidance* comes from, never the
safety limits.

## How it's wired

Point `agent.claude.context_dir` in `config/trading.yaml` at this `context/` directory
(absolute path). The bridge concatenates the files in priority order into Claude's system
prompt, then asks for one decision per closed bar. Set `agent.client: mock` to validate the
full loop with no LLM, then switch to `agent.client: claude`.

> The **enforced** risk numbers are always the ones in `config/trading.yaml`, not the prose
> in these notes. The bridge's RiskGate enforces the config; the notes only guide the brain.

## Legacy (unused) files

`personalities/`, `tools/ninjatrader.py`, and `cron/` are leftovers from an earlier
integration that ran this strategy on the Hermes Agent runtime with tool-calling. The
current Claude brain **does not call tools** — it only reasons and returns a decision, and
the bridge executes every order. These files are kept for reference (and a possible future
tool-using agent) but are not loaded or used by the Claude brain.
