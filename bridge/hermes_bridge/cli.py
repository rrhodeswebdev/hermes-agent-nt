"""Command-line entry point: `hermes-bridge serve` and `hermes-bridge replay`."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .config import load_config
from .models import Bar
from .replay_sim import ReplaySimulator


def _load_bars_csv(path: str) -> list[Bar]:
    bars: list[Bar] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append(
                Bar(
                    ts=float(row["ts"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0) or 0),
                    bid_volume=_optf(row.get("bid_volume")),
                    ask_volume=_optf(row.get("ask_volume")),
                )
            )
    return bars


def _optf(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    print(f"hermes-bridge serving on {cfg.server.host}:{cfg.server.port} "
          f"(agent={cfg.agent.client}, instrument={cfg.instrument.symbol} "
          f"{cfg.instrument.timeframe})")
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.agent:
        cfg.agent.client = args.agent
    bars = _load_bars_csv(args.bars)
    if len(bars) <= args.warmup:
        print(f"need more than warmup ({args.warmup}) bars; got {len(bars)}", file=sys.stderr)
        return 2
    sim = ReplaySimulator(cfg)
    report = sim.run(bars, warmup=args.warmup)
    if args.verbose:
        for line in report.decisions:
            print(line)
    print("---")
    print(report.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-bridge")
    default_cfg = str(Path("config/trading.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the bridge HTTP server")
    p_serve.add_argument("--config", default=default_cfg)
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_replay = sub.add_parser("replay", help="replay a CSV of bars through the engine")
    p_replay.add_argument(
        "bars",
        help="CSV columns: ts,open,high,low,close,volume[,bid_volume,ask_volume]",
    )
    p_replay.add_argument("--config", default=default_cfg)
    p_replay.add_argument("--agent", default=None, choices=["mock", "claude"])
    p_replay.add_argument("--warmup", type=int, default=50)
    p_replay.add_argument("--verbose", "-v", action="store_true")
    p_replay.set_defaults(func=_cmd_replay)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
