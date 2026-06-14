# CONTEXT — `hermes/` (trading knowledge & learned memory)

> Routed from the root [`CLAUDE.md`](../CLAUDE.md). Read that first for project-wide
> identity, rules, and naming. This file is the local map for the knowledge workspace.

## What this workspace is

The brain is **configured, not coded**. This workspace holds everything the decision brain
*knows* — its strategy framework, risk rules, and daily goal (plain English, no code) — plus
the self-improving memory it accumulates. The bridge loads these files into the Claude system
prompt; it never executes code from here. Edit a context file, restart the bridge, and the
agent trades the new way.

## Sub-areas

### `context/` — the framework (committed, edited by humans)

Loaded *verbatim* into the system prompt in both strategy modes.

| File | Teaches the brain |
| --- | --- |
| `HERMES.md` | the operating loop + non-negotiables (always use a stop, when unsure WAIT, one position). |
| `strategy.md` | how the active playbook is selected and the hard rules every setup obeys. |
| `order-flow.md` | reading buying vs. selling pressure (delta, absorption, exhaustion). |
| `price-action.md` | trend, structure, location. |
| `market-regime.md` | regime read from swing **structure** (HH/HL vs LH/LL vs contained). |
| `risk-management.md` | hard limits + behavioral rules (no revenge trading, think in R). |
| `daily-goal.md` | trade to a plan; bank green days; walk away at the max loss. |
| `strategies/trending/`, `strategies/ranging/` | **custom-mode** playbooks (`*.md`). Ship empty (`.gitkeep`); empty ⇒ the agent WAITs. |

> The **enforced** risk numbers always come from `config/trading.yaml`, not this prose.

### `prop-firms/` — prop-firm rulebooks (committed)

One plain-English `*.md` per prop firm (`topstep.md`, `apex.md`, …). Selecting a firm + account
in the dashboard loads **only that one file** into the prompt (on top of the framework) so the
brain trades within the firm's rules. Kept **outside** `context/` on purpose so the framework
loader's directory glob never concatenates every firm's file. The firm CATALOG (firms -> account
types -> sizes + numbers) lives in `config/prop-firms.yaml`; the account's daily-loss limit and
contract ceiling are **enforced** server-side (written into the live config the `RiskGate` reads),
while the eval target / trailing drawdown are guidance for the brain. See `prop-firms/README.md`.

### `generated/` — agent-authored playbooks (**gitignored, runtime**)

In agent mode the brain studies the chart's pre-session history and authors its own playbook.
Written here as `SYMBOL-YYYYMMDD-HHMM.md` plus `latest.md`, served at `GET /strategy`.
Regenerated every session — never commit, never hand-edit.

### `learned/` — self-improving memory (**live names gitignored, per-checkout**)

Updated by `bridge/hermes_bridge/reflect.py` after each closed trade.

- `lessons/` — distilled lessons applied to future decisions.
- `agent-notes.md` — the agent's running notes.
- `trader-profile.md` — your profile; reflection never overwrites it directly — it proposes
  `trader-profile.proposed.md` for you to review.
- `*.example.md` templates **are** committed; copy them to the live names to seed a checkout.
- `.history/` — timestamped backups of every overwrite (revertable).

### Legacy / unused (kept for reference, not loaded by the Claude brain)

`personalities/`, `tools/ninjatrader.py`, `cron/` — leftovers from the earlier tool-calling
Hermes Agent runtime. The current brain does **not** call tools; it reasons and returns a
`Decision`, and the bridge executes every order.

## Working rules (local)

- Context files are **plain English, no code.** Keep them declarative.
- Two strategy modes — **agent** (authors its own playbook) vs **custom** (your files under
  `context/strategies/**`). The NinjaTrader `UseAgentStrategies` toggle overrides
  `strategies.source` at runtime. Safety limits are identical either way.
- Never commit `generated/` or the live `learned/` names — they are per-checkout runtime state
  (see `.gitignore`). Do commit `context/` edits and `*.example.md` templates.
- Filenames are kebab-case `.md`.
