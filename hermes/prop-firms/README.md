# Prop-firm context files

Each `*.md` here is the plain-English rulebook for one prop firm. When you select that firm
(plus an account type and size) in the dashboard **Account** panel, the matching file is loaded
**verbatim** into the trading brain's system prompt — on top of the framework in
`hermes/context/`. The brain then trades *within* the firm's rules by judgment.

This directory is deliberately **outside** `hermes/context/` so the framework loader never
concatenates every firm's file into the prompt — only the **one** you select is loaded.

## Two layers, one selection

Selecting an account in the dashboard does two things:

1. **Enforced (server-side).** The account's daily loss limit and contract ceiling are written
   into the live config the `RiskGate` enforces. These are hard — the bridge will not place an
   order that breaks them. The numbers come from `config/prop-firms.yaml`, not this prose.
2. **Guidance (this file).** Everything a daily loss limit can't express — the cumulative
   trailing drawdown, the consistency / scaling / payout rules, the behaviours that get an
   account flagged — lives here so the brain honours them when it reasons.

> The bridge does **not** yet enforce a cumulative trailing drawdown (it has no high-water-mark
> primitive). Until it does, the trailing-drawdown number is guidance only: the brain is told
> the limit and asked to stay well clear. Keep your own stop discipline.

## Naming

Kebab-case `firm-name.md`, declarative prose, no code. Add a firm by adding a file here and a
matching entry (with `context_file:` pointing at it) in `config/prop-firms.yaml`.

> Rules and numbers change. These files describe each firm's program at the time of writing —
> verify against your firm's current terms.
