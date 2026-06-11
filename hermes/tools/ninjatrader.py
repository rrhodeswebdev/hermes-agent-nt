"""Hermes tools — NinjaTrader bridge.

Gives the Hermes agent eyes and hands on the market via the `hermes-bridge` HTTP
API. Drop this file into your Hermes `tools/` directory; it self-registers at import
time (Hermes auto-discovers any `tools/*.py` that calls `registry.register(...)`).

Every order placed here goes through the bridge's server-side RiskGate, so the agent
physically cannot bypass the position/risk/daily-goal limits — `nt_place_order` may
return `approved: false` with reasons, and that is the system working as designed.

Bridge location comes from env `HERMES_BRIDGE_URL` (default http://127.0.0.1:8787).
Uses only the Python standard library (urllib) so it adds no dependencies.

NOTE: `registry` is provided by the Hermes runtime. The import below matches Hermes'
documented `tools/registry.py` auto-discovery convention; adjust the import path if
your installed version exposes it differently.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

try:  # Hermes provides the tool registry at runtime.
    from tools import registry  # type: ignore
except Exception:  # pragma: no cover - allows linting/testing outside Hermes
    registry = None


BRIDGE_URL = os.getenv("HERMES_BRIDGE_URL", "http://127.0.0.1:8787").rstrip("/")
STRATEGY_ID = os.getenv("HERMES_STRATEGY_ID", "hermes-default")
_TIMEOUT = float(os.getenv("HERMES_BRIDGE_TIMEOUT", "8"))


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only)                                                   #
# --------------------------------------------------------------------------- #
def _get(path: str) -> dict:
    req = urllib.request.Request(BRIDGE_URL + path, method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Tool implementations                                                         #
# --------------------------------------------------------------------------- #
def nt_recent_bars(n: int = 50) -> dict:
    """Return the most recent `n` OHLCV bars the bridge has stored."""
    n = max(1, min(int(n), 500))
    return _get(f"/bars/recent?n={n}")


def nt_account_status() -> dict:
    """Return current position, average price, realized/unrealized P&L, trade count."""
    return _get("/account")


def nt_session_status() -> dict:
    """Return session state: P&L, halted flag/reason, daily-goal status, limits."""
    return _get("/session/status")


def nt_place_order(
    action: str,
    qty: int = 1,
    stop_ticks: int | None = None,
    target_ticks: int | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    reason: str = "agent",
) -> dict:
    """Place or exit an order. `action` is ENTER_LONG | ENTER_SHORT | EXIT.

    The bridge re-validates against the RiskGate; the result includes
    `approved` (bool) and `reasons`. A rejected order means a limit would be
    violated — read the reasons and adjust or WAIT.
    """
    body = {
        "strategy_id": STRATEGY_ID,
        "action": action,
        "qty": int(qty),
        "stop_ticks": stop_ticks,
        "target_ticks": target_ticks,
        "stop_price": stop_price,
        "target_price": target_price,
        "reason": reason,
    }
    return _post("/agent/command", body)


def nt_flatten(reason: str = "agent_flatten") -> dict:
    """Kill switch: flatten any open position and halt new entries for the day."""
    return _post("/control/flatten", {"reason": reason})


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #
def _bridge_reachable(*_args, **_kwargs):
    try:
        _get("/health")
        return True, "bridge reachable"
    except Exception as exc:  # noqa: BLE001
        return False, f"bridge unreachable at {BRIDGE_URL}: {exc}"


def register_all() -> None:
    if registry is None:
        return
    registry.register(
        name="nt_recent_bars",
        toolset="ninjatrader",
        schema={
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 500,
                                 "description": "How many recent bars to return."}},
        },
        handler=lambda args, **kw: nt_recent_bars(int(args.get("n", 50))),
        check_fn=_bridge_reachable,
    )
    registry.register(
        name="nt_account_status",
        toolset="ninjatrader",
        schema={"type": "object", "properties": {}},
        handler=lambda args, **kw: nt_account_status(),
        check_fn=_bridge_reachable,
    )
    registry.register(
        name="nt_session_status",
        toolset="ninjatrader",
        schema={"type": "object", "properties": {}},
        handler=lambda args, **kw: nt_session_status(),
        check_fn=_bridge_reachable,
    )
    registry.register(
        name="nt_place_order",
        toolset="ninjatrader",
        schema={
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"type": "string",
                           "enum": ["ENTER_LONG", "ENTER_SHORT", "EXIT"]},
                "qty": {"type": "integer", "minimum": 1},
                "stop_ticks": {"type": ["integer", "null"]},
                "target_ticks": {"type": ["integer", "null"]},
                "stop_price": {"type": ["number", "null"]},
                "target_price": {"type": ["number", "null"]},
                "reason": {"type": "string"},
            },
        },
        handler=lambda args, **kw: nt_place_order(
            action=args["action"],
            qty=int(args.get("qty", 1)),
            stop_ticks=args.get("stop_ticks"),
            target_ticks=args.get("target_ticks"),
            stop_price=args.get("stop_price"),
            target_price=args.get("target_price"),
            reason=args.get("reason", "agent"),
        ),
        check_fn=_bridge_reachable,
    )
    registry.register(
        name="nt_flatten",
        toolset="ninjatrader",
        schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
        handler=lambda args, **kw: nt_flatten(args.get("reason", "agent_flatten")),
        check_fn=_bridge_reachable,
    )


register_all()
