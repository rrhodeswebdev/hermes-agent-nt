# Reading the order book (Level 2 / market depth)

These features appear **only when Level 2 is on and the feed is streaming depth**. When
they are absent, this whole section does not apply — trade the tape (delta) as usual.

The book is **resting liquidity** — orders waiting, not orders filled. It can be pulled or
spoofed. So weight it **with** the executed tape (`recent_delta` / `delta_ratio`), never
instead of it. Depth is context, delta is proof.

## The features

- **`depth_imbalance`** (~[-1, +1]) — resting bid size vs ask size over the top levels.
  Positive = more resting bids (support-heavy); negative = ask-heavy (resistance-heavy).
  Like `delta_ratio`, it is a *ratio* — session-independent. A persistent one-sided book
  behind a move in your direction is confirmation; a book stacked against you is a caution.
- **`depth_walls`** — outsized single resting orders as `[price, size, side]`. A `bid` wall
  is potential support; an `ask` wall potential resistance. Walls attract price (liquidity)
  and can either hold (absorption) or get swept — do not treat a wall as a guaranteed floor
  or ceiling; watch whether delta is being absorbed at it.
- **`absorption`** — e.g. `bid_absorption@5123.25`: price has repeatedly tested a resting
  wall that keeps refreshing instead of breaking. Support/resistance holding under pressure.
  A classic reversal tell **when delta into the level is failing to extend** — pair the two.
- **`spread`**, **`top_bid_size`**, **`top_ask_size`** — the touch. A thin top of book means
  slippage risk and fast moves; a thick touch means the level is defended.

## How to use it

- **Joining a move:** want the book leaning your way (`depth_imbalance` same sign as your
  trade) and no large opposing wall directly in your path.
- **Fading into a level:** `absorption` at the level + stalling delta is the setup; a wall
  with delta still driving through it is not — that wall may be about to break.
- **Never** let a depth read alone trigger or veto a trade. It sharpens a decision the
  structure and tape already support. If depth and delta disagree, trust delta.
