"""Shared plumbing for isolated one-shot `claude -p` calls (decisions + reflection).

`run_claude_oneshot` runs Claude Code in headless print mode on the user's
subscription, isolated via --safe-mode, tool-less, with the system prompt in a temp
file (Windows command-line limit) and the user message on stdin. `extract_structured`
pulls the schema-validated object out of the JSON envelope (preferring
`structured_output`, falling back to a JSON block in `result`).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .agent_client import _extract_json
from .config import ClaudeClientConfig


def run_claude_oneshot(c: ClaudeClientConfig, system: str, user: str,
                       json_schema: str | None = None, model: str | None = None,
                       timeout_s: float | None = None) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        tmp.write(system)
        tmp.close()
        cmd = [
            c.claude_bin, "-p",
            "--model", model or c.model,
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt-file", tmp.name,
        ]
        if json_schema:
            cmd += ["--json-schema", json_schema]
        if c.safe_mode:
            cmd.append("--safe-mode")
        cmd.extend(c.extra_args)
        out = subprocess.run(
            cmd, input=user, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout_s if timeout_s is not None else c.timeout_s,
        )
        return out.stdout
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass


def extract_structured(reply: str) -> dict | None:
    """Return the schema-validated object from a `claude --output-format json` reply."""
    try:
        env = json.loads(reply)
    except Exception:  # noqa: BLE001
        env = None
    if isinstance(env, dict):
        if env.get("is_error"):
            return None
        so = env.get("structured_output")
        if isinstance(so, dict):
            return so
        res = env["result"] if "result" in env else reply
    else:
        res = reply
    if isinstance(res, dict):
        return res
    block = _extract_json(str(res))
    if block is None:
        return None
    try:
        data = json.loads(block)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None
