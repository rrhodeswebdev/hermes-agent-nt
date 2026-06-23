"""Shared plumbing for `claude -p` calls — one-shot and persistent.

`run_claude_oneshot` runs Claude Code in headless print mode on the user's
subscription, isolated via --safe-mode, tool-less, with the system prompt in a temp
file (Windows command-line limit) and the user message on stdin. `ClaudeSession`
keeps one such child alive across requests (stream-json in/out) so the 1-3s CLI
cold start and the system-prompt upload are paid once, not per decision.
`extract_structured` pulls the schema-validated object out of the JSON envelope
(preferring `structured_output`, falling back to a JSON block in `result`) — both
call styles return that same envelope shape.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import ClaudeClientConfig
from .models import BrainTimeout


def _thinking_env(c: ClaudeClientConfig) -> dict[str, str] | None:
    """Subprocess env that caps extended thinking via MAX_THINKING_TOKENS.

    Thinking tokens are the dominant decision-latency cost. Returns None (inherit the
    parent env unchanged) when max_thinking_tokens is None, so an uncapped config behaves
    exactly as before.
    """
    if c.max_thinking_tokens is None:
        return None
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = str(c.max_thinking_tokens)
    return env


def _fallback_model_args(c: ClaudeClientConfig) -> list[str]:
    """`--fallback-model <csv>` for a per-model overload, or [] when unconfigured. The CLI
    routes a 529/overload of the primary tier down this list inside a single call."""
    models = [m for m in (c.fallback_models or []) if m]
    return ["--fallback-model", ",".join(models)] if models else []


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
        cmd.extend(_fallback_model_args(c))
        cmd.extend(c.extra_args)
        budget = timeout_s if timeout_s is not None else c.timeout_s
        try:
            out = subprocess.run(
                cmd, input=user, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=_thinking_env(c),
                timeout=budget,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrainTimeout(budget) from exc
        if out.returncode != 0:
            # Surface the real CLI failure (auth expiry, bad flag, …); empty stdout
            # would otherwise read downstream as "model returned nothing".
            err = (out.stderr or "").strip()
            msg = f"claude CLI exited {out.returncode}: {err[:400] or '(no stderr)'}"
            print(f"[claude] {msg}", flush=True)
            raise RuntimeError(msg)
        return out.stdout
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass


class ClaudeSession:
    """One long-lived `claude -p --input-format stream-json` child.

    The system prompt and --json-schema are fixed at spawn and paid once; each
    `ask()` writes one user turn to stdin and reads stream-json lines until the
    `result` envelope (the same shape `run_claude_oneshot` returns). A timeout
    kills the child and raises BrainTimeout; any other protocol failure raises and
    the caller is expected to drop the session (fall back to a one-shot).
    """

    def __init__(self, c: ClaudeClientConfig, system: str,
                 json_schema: str | None = None) -> None:
        self._c = c
        self.system = system  # fixed at spawn; _session_ask recycles when it changes
        self._lock = threading.Lock()  # one in-flight turn at a time
        self.turns = 0  # user turns asked; drives max_session_turns recycling
        tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                          encoding="utf-8")
        tmp.write(system)
        tmp.close()
        self._prompt_path = tmp.name
        # stderr goes to a temp file: DEVNULL would discard the actual CLI error
        # (auth expiry, bad flag) and an undrained PIPE could deadlock the child.
        err = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                          encoding="utf-8")
        self._stderr_path = err.name
        cmd = [
            c.claude_bin, "-p",
            "--model", c.model,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",  # stream-json output requires it in print mode
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt-file", self._prompt_path,
        ]
        if json_schema:
            cmd += ["--json-schema", json_schema]
        if c.safe_mode:
            cmd.append("--safe-mode")
        cmd.extend(_fallback_model_args(c))
        cmd.extend(c.extra_args)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, text=True, encoding="utf-8",
            errors="replace", env=_thinking_env(c),
        )
        err.close()  # the child holds its own fd; ours is only for the path
        # stdout is drained by a thread so ask() can enforce a deadline.
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True,
                         name="claude-session-reader").start()

    def _read_loop(self) -> None:
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF sentinel

    def alive(self) -> bool:
        return self._proc.poll() is None

    def ask(self, user: str, timeout_s: float | None = None) -> str:
        budget = timeout_s if timeout_s is not None else self._c.timeout_s
        deadline = time.monotonic() + budget
        with self._lock:
            if not self.alive():
                raise RuntimeError("claude session child has exited")
            msg = {"type": "user",
                   "message": {"role": "user",
                               "content": [{"type": "text", "text": user}]}}
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise BrainTimeout(budget)
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    self.close()
                    raise BrainTimeout(budget) from None
                if line is None:
                    tail = self._stderr_tail()
                    msg = ("claude session closed mid-turn (EOF)"
                           + (f": {tail}" if tail else ""))
                    print(f"[claude] {msg}", flush=True)
                    raise RuntimeError(msg)
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001 — skip any non-JSON noise
                    continue
                if obj.get("type") == "result":
                    self.turns += 1
                    return line

    def _stderr_tail(self, limit: int = 400) -> str:
        try:
            text = Path(self._stderr_path).read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text[-limit:]

    def close(self) -> None:
        try:
            self._proc.kill()
        except Exception:  # noqa: BLE001
            pass
        for path in (self._prompt_path, self._stderr_path):
            try:
                Path(path).unlink()
            except OSError:
                pass


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> str | None:
    m = _JSON_FENCE.search(text)
    if m:
        return m.group(1)
    # Fallback: first balanced-looking object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


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
