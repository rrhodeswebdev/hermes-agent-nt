"""Command-line entry point: `hermes-bridge serve`, `replay`, `check`, `config-dump`."""

from __future__ import annotations

import argparse
import csv
import shlex
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

    # Console safety: agent rationales contain Unicode (—, ≈, →). Default Windows stdout
    # is cp1252 and raises UnicodeEncodeError on print(), 500-ing /ingest/bar. Force UTF-8.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    print(f"hermes-bridge serving on {cfg.server.host}:{cfg.server.port} "
          f"(agent={cfg.agent.client}, instrument={cfg.instrument.symbol} "
          f"{cfg.instrument.timeframe})")
    uvicorn.run(create_app(cfg, config_path=args.config),
                host=cfg.server.host, port=cfg.server.port)
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


def _cmd_check(args: argparse.Namespace) -> int:
    """Ping the configured brain through the SAME plumbing the bridge uses per bar
    (--json-schema, --tools "", --safe-mode, system-prompt file, thinking cap), so a
    pass here means real decisions can flow — not just that the CLI launches."""
    cfg = load_config(args.config)
    if cfg.agent.client != "claude":
        print(f"agent={cfg.agent.client}: deterministic rules, no LLM to check")
        return 0
    from .claude_agent import DECISION_JSON_SCHEMA
    from .claude_cli import extract_structured, run_claude_oneshot

    c = cfg.agent.claude
    try:
        reply = run_claude_oneshot(
            c,
            'Connectivity check. Reply with action WAIT and rationale "pong".',
            "ping",
            json_schema=DECISION_JSON_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 — a check should report, not traceback
        print(f"claude check FAILED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    data = extract_structured(reply)
    if data is None:
        print(f"claude check FAILED: no structured output in reply: {reply[:200]!r}",
              file=sys.stderr)
        return 1
    print(f"claude check OK (model={c.model}): {data}")
    return 0


def _cmd_config_dump(args: argparse.Namespace) -> int:
    """Resolved config (with env overrides) as shell-evalable KEY=VALUE lines.

    start.sh evals this instead of duplicating config knowledge in shell."""
    cfg = load_config(args.config)
    c = cfg.agent.claude
    pairs = {
        "CLIENT": cfg.agent.client,
        "CBIN": c.claude_bin,
        "CMODEL": c.model,
        "HOST": cfg.server.host,
        "PORT": cfg.server.port,
        "SID": cfg.strategy_id,
        "SYM": cfg.instrument.symbol,
        "TF": cfg.instrument.timeframe,
        "ACCT": cfg.execution.account,
        "LIVE": "1" if cfg.execution.allow_live else "0",
    }
    for key, value in pairs.items():
        print(f"{key}={shlex.quote(str(value))}")
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

    p_check = sub.add_parser(
        "check", help="ping the configured decision brain through the real call path"
    )
    p_check.add_argument("--config", default=default_cfg)
    p_check.set_defaults(func=_cmd_check)

    p_dump = sub.add_parser(
        "config-dump", help="print the resolved config as shell-evalable KEY=VALUE lines"
    )
    p_dump.add_argument("--config", default=default_cfg)
    p_dump.set_defaults(func=_cmd_config_dump)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
