# The Super Simple Setup Guide 🤖

Hi! This guide sets up your trading robot, step by step, in **easy words**. You can do
this! 💪

> 🟢 **Good news first:** the robot plays with **pretend money** (called "Sim"). It's a
> practice game — no real money can be lost while it says **Sim**.

---

## 🧩 What are all the parts?

Think of it like a small team of helpers:

| The part | Think of it like… | What it does |
| --- | --- | --- |
| **NinjaTrader 8** | A TV that shows the price going up and down | Shows the market chart (lives on the Windows side) |
| **The Strategy** (`HermesBridgeStrategy`) | A mail carrier 📬 | Every minute it mails the new price to the robot, and does what the robot says (buy / sell / wait) |
| **The Bridge** | The robot's desk + a safety guard 👮 | Keeps the rule book, checks every order is safe, and remembers the score |
| **Claude** (the robot's brain) | The robot's brain 🧠 | The Claude Code CLI on your computer — it does the thinking, on **your own Claude subscription** (no API key, no per-token bill) |
| **The Dashboard** | A window 🪟 | Lets *you* watch what the robot is thinking |

The robot's job: look at the price, then say **BUY**, **SELL**, or **WAIT** — using rules
you can read and change.

---

## ✅ Before you start, you need these

1. A **Mac** computer (you have this!).
2. **NinjaTrader 8** running in Windows (you run it in Parallels — that's a Windows
   "computer inside your Mac") with a **Sim101** (or **Playback**) account and **real-time** data.
3. The **Claude Code CLI** installed and logged in (this is the brain — it runs on your
   own **Claude subscription**, so make sure you can sign in).
4. **[`uv`](https://docs.astral.sh/uv/)** — a small tool that builds the bridge's Python
   environment.
5. The **project folder** at `/Users/hypawolf/code/hermes-trading-agent` (you have this too!).

---

## Step 1 — Get the robot brain 🧠

The brain is the **Claude Code CLI** (the `claude` app). Good news: you very likely
**already have it** — that's the app you're probably reading this with!

Open the **Terminal** app on your Mac and check by typing this, then pressing **Enter**:

```bash
claude --version
```

If it shows a version number, the brain is ready — skip to Step 2! ✅

Only if it says "command not found", install it from the official page:
**<https://claude.com/claude-code>** — then run `claude --version` again.

**How do you know it worked?** `claude --version` shows a version number.

> No subscription handy? You can still run everything on the **mock** brain (deterministic
> rules, no LLM) — see Step 3.

---

## Step 2 — Turn on the desk (the Bridge) 🪑

First make sure you're **logged in** so the brain can think — in the Terminal, type
`claude`, and if it asks you to sign in, type `/login` and follow the steps (or just
`/exit` if it's already logged in).

Now open a terminal in the **project folder** and start the desk. On a **Mac**:

```bash
./start.sh
```

This builds the bridge on first run, pings Claude once to prove the brain answers, then
starts serving. On **Windows**, use `.\start.ps1 -CheckClaude` instead.

**Worked?** It prints `serving on 0.0.0.0:8787` and exactly what to type into NinjaTrader
(host, `BridgePort`, `StrategyId`). **Leave this window open** — the desk stays on while
the robot works.

> Want a dry run with no LLM first? `./start.sh --mock` (Mac) or `.\start.ps1 -Mock`
> (Windows).

---

## Step 3 — Pick the brain & the rules 📒

The brain is set in **`config/trading.yaml`** under `agent.client`:

- **`claude`** — your Claude subscription (the **default**).
- **`mock`** — deterministic rules, no LLM (great for a dry run).

The robot's *trading rules* are plain-English notes in **`hermes/context/`** (like
`strategy.md`). Want it to trade differently? **Change those notes** — no code needed.

Your **personal** values (like your project path) can go in a gitignored
`config/trading.local.yaml` so they don't get overwritten. The robot also **learns**: it
writes lessons into `hermes/learned/` and reviews them between bars to improve over time.

> ⚠️ If you change any config or context file, **restart the Bridge** (Step 2) so the robot
> notices.

**How do you know it worked?** Run the practice test:

```bash
make test
```

If it says something like **"60 passed"**, you're golden! 🌟

---

## Step 4 — Put the mail carrier in NinjaTrader 📬

In **NinjaTrader**:

1. Open the **NinjaScript Editor** (Tools → NinjaScript Editor).
2. Make a **New Strategy** → paste `ninjatrader/HermesBridgeStrategy.cs` → press **F5** to
   compile. Fix any red messages if they pop up.
3. Open a **2-minute MNQ** chart (2–3m gives Claude time to think; 1m is tight).
4. Right-click the chart → **Strategies…** → add **HermesBridgeStrategy**, and set:

| Box | Value |
| --- | --- |
| `BridgeHost` | `127.0.0.1` |
| `BridgePort` | `8787` |
| `StrategyId` | `hermes-default` |
| `HttpTimeoutMs` | `115000` ← must be longer than the agent's think time |
| `AllowLive` | `false` ← keep it OFF for pretend money |
| Account | `Sim101` (or a **Playback** account — both are pretend money) |

Whatever account you pick here is the one the robot uses — the strategy tells the desk
which account you chose, so the watching window shows it. You don't set the account anywhere
else.

1. Make sure the chart is on your **real-time** feed (not delayed), then click **Enable**. 🟢

**Worked?** The **NinjaScript Output** window shows a line like
`Hermes: sent … historical bars` — the mail carrier just shipped the price history to the
desk! 🎉

---

## Step 5 — Add the watching window 🪟

Two ways — pick either (or both):

**Way A — In a browser (easiest):** open `http://127.0.0.1:8787/` — a live page with the
robot's position, score, and latest thought. ✨

**Way B — Right on the chart:** in the NinjaScript Editor, make a **New Indicator** → paste
`ninjatrader/HermesDashboard.cs` → press **F5**. Right-click a chart → **Indicators…** →
add **HermesDashboard** (`BridgeHost` `127.0.0.1`, `BridgePort` `8787`). It also draws the
agent's **support/resistance lines** on the chart.

---

## Step 6 — Watch it work! 👀

That's it — you did it! 🥳 Now just watch.

- Most of the time it says **WAIT**. That is **normal and good** — a smart trader waits for
  a *really* good moment. It's being patient, not broken.
- On a great setup it says **BUY** or **SELL**, and you'll see a trade with a safety
  **stop** appear on the chart.
- When the day's goal or the loss limit is reached, it **closes everything and stops** until
  tomorrow.

The window shows its reason every time, like:
*"Uptrend, but price is too close to resistance — waiting for a better spot."*

---

## 🛑 The big red STOP button

To make it **stop and close everything right now**, run this in a terminal:

```bash
curl -X POST http://127.0.0.1:8787/control/flatten
```

This is the **kill switch** — it flattens everything and stops new trades for the day. To
let it trade again later:

```bash
curl -X POST http://127.0.0.1:8787/control/resume
```

To **stop the desk completely**, click the Bridge window and press **Ctrl + C**.

---

## 🦺 Safety rules (please read!)

- It uses **pretend money**. Keep `AllowLive` set to **false** and the account on a
  simulated one — **Sim101** or a **Playback** account both work (the strategy tells the
  desk which one you picked, so the window shows it). Do **not** switch to real money until
  you've watched it for a long time and
  fully understand it.
- A **safety guard** (the RiskGate) checks *every* order, server-side, and blocks anything
  too big or too risky. That's the guard doing its job!
- The robot is a **helper**, not magic. The trading rules are a starting example — *not*
  promised to make money. You are the boss.

---

## 😟 Oops! Something's not working

| What you see | What it probably means | What to do |
| --- | --- | --- |
| Nothing in NinjaTrader's output | That's **normal** — it only prints history/errors | Watch the **dashboard** instead |
| "A task was canceled" | The robot needed more time to think | Raise `HttpTimeoutMs` (e.g. `115000`) and re-enable the strategy |
| Dashboard "data age" is big (like 600s) | Delayed chart data, or the strategy needs re-enabling after a bridge restart | Switch to **real-time** data and **re-enable** the strategy |
| "bridge unreachable" | The desk (Bridge) isn't running | Do Step 2 again |
| It never trades | It's being **patient** (usually fine), or the market is quiet | Let it run; read the dashboard reasons |

---

## 🧠 The one-minute review

1. Brain → check `claude --version` (you probably already have it), run `claude` and
   `/login` with your Claude subscription (or use **mock**).
2. Desk → `./start.sh` (Mac) or `.\start.ps1 -CheckClaude` (Windows).
3. (Optional) pick the brain + edit rules → `config/trading.yaml` and `hermes/context/`.
4. Mail carrier → compile `HermesBridgeStrategy.cs`, enable on a **2m MNQ** chart.
5. Window → open `http://127.0.0.1:8787/`.
6. Watch! And use the **STOP button** if you ever need to.

You're all set. Have fun, and stay safe with **pretend money** first! 🎈
