# The Super Simple Setup Guide 🤖

Hi! This guide shows you how to set up your trading robot, step by step.
We will use **easy words** and go **slow**. You can do this! 💪

> 🟢 **Good news first:** This robot plays with **pretend money** (called "Sim").
> It is a practice game. No real money can be lost while it says **Sim**.

---

## 🧩 What are all the parts?

Think of it like a team of helpers. Here is who does what:

| The part | Think of it like… | What it does |
|---|---|---|
| **NinjaTrader** | A TV that shows the price going up and down | Shows the market chart (lives on the Windows side) |
| **The Strategy** (`HermesBridgeStrategy`) | A mail carrier 📬 | Every minute it mails the new price to the robot, and does what the robot says (buy / sell / wait) |
| **The Bridge** | The robot's desk + a safety guard 👮 | Keeps the rule book, checks every order is safe, and remembers the score |
| **Hermes** | The robot's brain 🧠 | The thinking program you install |
| **Codex** (your ChatGPT login) | Brain power ⚡ | Lets the brain actually think |
| **The Dashboard** | A window 🪟 | Lets *you* watch what the robot is thinking |

The robot's job: look at the price, then say **BUY**, **SELL**, or **WAIT** — using
rules you can read and change.

---

## ✅ Before you start, you need 4 things

1. A **Mac** computer (you have this!).
2. **NinjaTrader 8** running in Windows (you run it in Parallels — that's a Windows
   "computer inside your Mac").
3. A **ChatGPT / Codex account** (this gives the brain its thinking power).
4. The **project folder** at `/Users/hypawolf/code/hermes-trading-agent` (you have this too!).

---

## Step 1 — Install the robot brain 🧠

Open the **Terminal** app on your Mac. Copy this line, paste it, and press **Enter**:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

This downloads the brain (called **Hermes**). It takes a few minutes. Let it finish.

**How do you know it worked?** Type `hermes` and press Enter. If a little chat starts,
the brain is alive! (Type `exit` or press Ctrl+C to leave the chat.)

---

## Step 2 — Give the brain its thinking power ⚡

The brain needs a way to *think*. We use your **Codex** (ChatGPT) login. In the Terminal:

```bash
hermes auth add openai-codex
```

A web page will pop up. **Log in with your ChatGPT account.** That's it!

Now pick which brain it uses:

```bash
hermes model
```

A little menu shows up. Choose **OpenAI Codex**. Done. ✅

**How do you know it worked?** Type `hermes`, say "hi", and see if it answers back.

---

## Step 3 — Build the robot's desk 🪑

The "desk" is called the **Bridge**. It does the safety checking and keeps score.
In the Terminal, go to the project folder and set it up:

```bash
cd /Users/hypawolf/code/hermes-trading-agent
make setup
```

This builds everything the desk needs. Wait for it to finish.

**How do you know it worked?** Run the practice test:

```bash
make test
```

If it says something like **"38 passed"**, you're golden! 🌟

---

## Step 4 — Tell the robot how to trade 📒

The rule book is a file called **`config/trading.yaml`**. You can open it in any text
editor. Inside, you can change easy things like:

- **What to trade** (`symbol` — right now it's **MNQ**, the tiny Nasdaq contract).
- **How much it can risk** (`max_risk_per_trade`).
- **The daily goal** (`profit_target`) and **stop-for-the-day** (`max_daily_loss`).

The robot's *trading brain rules* live in the **`hermes/context/`** folder. These are
just notes written in plain English (like `strategy.md`). If you want the robot to
trade differently, **change those notes** — no code needed!

> ⚠️ If you change any of these files, you must **restart the Bridge** (Step 5) for
> the robot to notice.

---

## Step 5 — Turn on the desk (start the Bridge) 🟢

In the Terminal:

```bash
cd /Users/hypawolf/code/hermes-trading-agent
./scripts/run_bridge.sh
```

Leave this window **open** — the desk needs to stay on while the robot works.

**How do you know it worked?** It will say something like
`serving on 0.0.0.0:8787`. That means the desk is awake and listening. 👂

---

## Step 6 — Put the mail carrier in NinjaTrader 📬

Now switch to **NinjaTrader** (in your Windows/Parallels window).

1. Open the **NinjaScript Editor** (Tools menu → NinjaScript Editor).
2. Make a **New Strategy**, then paste in the file
   `ninjatrader/HermesBridgeStrategy.cs`.
3. Press **F5** to **Compile** (that means "build it"). Fix any red messages if they pop up.
4. Open a **chart** for **MNQ** on a **1-minute** time.
5. Right-click the chart → **Strategies…** → add **HermesBridgeStrategy**.
6. Set these boxes:

| Box | Type this |
|---|---|
| `BridgeHost` | `192.168.1.16` |
| `BridgePort` | `8787` |
| `StrategyId` | `hermes-default` |
| `HttpTimeoutMs` | `45000` |
| `AllowLive` | `false` ← keep it OFF for pretend money |
| Account | `Sim101` |

7. Make sure the chart is using your **real-time data** (not delayed), then click **Enable**. 🟢

**How do you know it worked?** Look at the **NinjaScript Output** window. You should
see a line like **"Hermes: sent … historical bars"**. That means the mail carrier
just mailed the price history to the desk! 🎉

---

## Step 7 — Add the watching window 🪟

You have **two ways** to watch the robot think. Pick either (or both!).

**Way A — On your computer screen (easiest):**
Open a web browser and go to:
- On the **Mac**: `http://localhost:8787/`
- In the **Windows** window: `http://192.168.1.16:8787/`

You'll see a live page with the robot's position, score, and its latest thought. ✨

**Way B — Right on the chart (in NinjaTrader):**
1. In the NinjaScript Editor, make a **New Indicator** and paste in
   `ninjatrader/HermesDashboard.cs`. Press **F5** to compile.
2. Right-click a chart → **Indicators…** → add **HermesDashboard**.
3. Set `BridgeHost` = `192.168.1.16` and `BridgePort` = `8787`.

Now a little panel shows up on the chart with the robot's thoughts!

---

## Step 8 — Watch it work! 👀

That's it — you did it! 🥳 Now just watch.

- Most of the time the robot says **WAIT**. That is **normal and good** — a smart
  trader waits for a *really* good moment. It is being patient, not broken.
- When it sees a great setup, it will say **BUY** or **SELL**, and you'll see a trade
  with a safety **stop** appear on the chart.
- When the day's goal or the stop-for-the-day is reached, it **closes everything and
  stops** until tomorrow.

The watching window shows the robot's reason every time, like:
*"Uptrend, but the price is too close to the top — waiting for a better spot."*

---

## 🛑 The big red STOP button

If you ever want it to **stop and close everything right now**, type this in a
Terminal:

```bash
curl -X POST http://localhost:8787/control/flatten
```

This is the **kill switch**. It sells everything and stops new trades for the day.
To let it trade again later:

```bash
curl -X POST http://localhost:8787/control/resume
```

To **stop the desk completely**, click the Terminal window running the Bridge and
press **Ctrl + C**.

---

## 🦺 Safety rules (please read!)

- It uses **pretend money (Sim)**. Keep `AllowLive` set to **false** and the account
  on **Sim101**. Do **not** switch to real money until you have watched it for a long
  time and you fully understand it.
- A **safety guard** checks *every* order before it happens. It will block trades that
  are too big or too risky. That is the guard doing its job!
- The robot is a **helper**, not magic. The trading rules are a starting example. It is
  *not* promised to make money. You are the boss.

---

## 😟 Oops! Something's not working

| What you see | What it probably means | What to do |
|---|---|---|
| Nothing in NinjaTrader's output | That's **normal**! It only prints history or errors. | Watch the **dashboard** instead — that's where the thinking shows. |
| "A task was canceled" | The robot needed more time to think | Set `HttpTimeoutMs` to `45000` and re-enable the strategy. |
| Dashboard says "data age" is big (like 600s) | Your chart is on **delayed** data | Switch the chart to your **real-time** feed and re-enable. |
| Dashboard says "bridge unreachable" | The desk (Bridge) isn't running | Do Step 5 again to start it. |
| It never trades | It's being **patient** (usually fine), or the market is quiet | Let it run. Check the dashboard reasons to see why it's waiting. |

---

## 🧠 The one-minute review

1. Install the brain → `curl … | bash`
2. Give it thinking power → `hermes auth add openai-codex` then `hermes model`
3. Build the desk → `make setup`
4. (Optional) change the rules → `config/trading.yaml` and `hermes/context/`
5. Start the desk → `./scripts/run_bridge.sh`
6. Add the mail carrier → compile `HermesBridgeStrategy.cs`, enable on the MNQ chart
7. Add the window → open `http://localhost:8787/`
8. Watch! And use the **STOP button** if you ever need to.

You're all set. Have fun, and stay safe with **pretend money** first! 🎈
