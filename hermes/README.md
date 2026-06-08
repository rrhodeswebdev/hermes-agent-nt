# Hermes customization

These files turn a stock Hermes Agent install into **Hermes the trader**. They use
Hermes' native extension points so you are *customizing*, not forking, the agent.

| This repo | Goes into | Purpose |
|-----------|-----------|---------|
| `context/*.md` | your Hermes **project context** (or `~/.hermes` context dir) | the trading knowledge, strategy, risk rules, daily goal — injected into every conversation |
| `personalities/hermes-trader.md` | `~/.hermes/personalities/hermes-trader.md` | the disciplined-trader voice (`/personality hermes-trader`) |
| `tools/ninjatrader.py` | your Hermes **`tools/`** directory | the `nt_*` tools (auto-discovered via `registry.register`) |
| `cron/trading-session.yaml` | `~/.hermes/cron/` | optional session open/close checks |

## Install

A helper script copies these into place (and installs Hermes if needed):

```bash
scripts/install_hermes.sh        # see the repo root
```

…or do it manually:

```bash
# 1) Context files — make them part of every conversation. Easiest: keep them in the
#    project you launch Hermes from, or copy into your Hermes context directory.
cp hermes/context/*.md   <your-hermes-project-or-context-dir>/

# 2) Personality
mkdir -p ~/.hermes/personalities
cp hermes/personalities/hermes-trader.md ~/.hermes/personalities/

# 3) Tools — into the Hermes tools/ directory so registry auto-discovery picks them up
cp hermes/tools/ninjatrader.py <hermes-install>/tools/

# 4) (optional) Cron
mkdir -p ~/.hermes/cron
cp hermes/cron/trading-session.yaml ~/.hermes/cron/
```

## Enable the toolset

The `nt_*` tools register under the **`ninjatrader`** toolset. A tool is only exposed
to the agent if its toolset is active for the agent (per Hermes' `AGENTS.md`). Add
`ninjatrader` to your agent's toolset/config, or include it in the base bundle, so the
agent can actually call `nt_recent_bars`, `nt_place_order`, etc.

## Configure the bridge connection

The tools read these env vars (set them in `~/.hermes/.env` or your shell):

```bash
HERMES_BRIDGE_URL=http://127.0.0.1:8787   # where hermes-bridge is serving
HERMES_STRATEGY_ID=hermes-default         # must match config/trading.yaml strategy_id
HERMES_BRIDGE_TIMEOUT=8
```

## Two ways to run the decision loop

1. **Bridge-driven (default in this repo):** the bridge calls the agent each closed
   bar (set `agent.client: hermes` in `config/trading.yaml`). The agent reasons using
   these context files and acts via the `nt_*` tools. Start with `agent.client: mock`
   to validate the full loop with no LLM, then switch to `hermes`.
2. **Agent-driven (advanced):** use the cron schedule (or a Hermes loop) to wake the
   agent, which pulls bars with `nt_recent_bars` and acts. Event timing is looser than
   bridge-driven, so prefer option 1 for per-bar trading.
