"""Generate a deterministic synthetic bar series for the replay demo/tests.

Produces an uptrend with periodic pullbacks toward the moving averages so the
MockAgentClient's trend-pullback logic actually triggers entries. Pure stdlib so
it runs with any Python. Output: sample_bars.csv next to this file.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

START_TS = 1_700_000_000  # fixed epoch so runs are reproducible
STEP_S = 300              # 5-minute bars
N = 400


def main() -> None:
    rows = []
    for i in range(N):
        trend = i * 0.5
        wave = math.sin(i / 8.0) * 6.0
        base = 4000.0 + trend + wave
        drift = 1.0 if math.cos(i / 8.0) > 0 else -0.8
        open_ = base
        close = base + drift
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        volume = 1000 + (i % 5) * 50
        rows.append(
            {
                "ts": START_TS + i * STEP_S,
                "open": round(open_, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": volume,
            }
        )
    out = Path(__file__).with_name("sample_bars.csv")
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} bars -> {out}")


if __name__ == "__main__":
    main()
