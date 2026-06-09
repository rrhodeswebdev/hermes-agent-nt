#!/usr/bin/env bash
# ============================================================================
# Hermes × NinjaTrader 8 — one-command startup (Mac side).
#
# This starts EVERYTHING the Mac is responsible for: the Python bridge server
# (which also serves the web dashboard, health, session status, and control
# endpoints). It reads config/trading.yaml to decide how the decision brain is
# reached, sets up the venv on first run, waits until the bridge is healthy,
# prints exactly what to plug into NinjaTrader, then streams the logs.
#
# You start the NinjaTrader side yourself (compile + enable HermesBridgeStrategy
# on a Sim chart) — this script prints the host/port/StrategyId to use.
#
# Usage:
#   ./start.sh                 # start the bridge using config/trading.yaml
#   ./start.sh --mock          # force the deterministic mock brain (no LLM)
#   ./start.sh --check-hermes  # also do a live `hermes -z` ping before serving
#   HERMES_BRIDGE_PORT=9000 ./start.sh   # env overrides are honored
#
# Ctrl-C cleanly stops the bridge.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$REPO_ROOT/bridge"
CONFIG="$REPO_ROOT/config/trading.yaml"
VENV="$BRIDGE_DIR/.venv"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_REPO="$HERMES_HOME/hermes-agent"

FORCE_MOCK=0
CHECK_HERMES=0
PASSTHRU=()
for arg in "$@"; do
  case "$arg" in
    --mock)         FORCE_MOCK=1 ;;
    --check-hermes) CHECK_HERMES=1 ;;
    *)              PASSTHRU+=("$arg") ;;
  esac
done

say()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || die "uv not found — install it: https://docs.astral.sh/uv/"

# --------------------------------------------------------------------------- #
# 1. Bridge venv (create on first run).                                        #
# --------------------------------------------------------------------------- #
if [[ ! -x "$VENV/bin/hermes-bridge" ]]; then
  say "Setting up the bridge venv (first run)…"
  ( cd "$BRIDGE_DIR" && uv venv --python 3.11 .venv && uv pip install --python .venv -e ".[dev]" )
  ok "Bridge venv ready."
fi

# --------------------------------------------------------------------------- #
# 2. Read the resolved config authoritatively (same loader + env overrides).   #
# --------------------------------------------------------------------------- #
if [[ "$FORCE_MOCK" == "1" ]]; then export HERMES_BRIDGE_AGENT=mock; fi
eval "$(cd "$BRIDGE_DIR" && "$VENV/bin/python" - "$CONFIG" <<'PY'
import sys
from hermes_bridge.config import load_config
c = load_config(sys.argv[1])
print(f"CLIENT={c.agent.client!s}")
print(f"HMODE={c.agent.hermes.mode!s}")
print(f"HBIN={c.agent.hermes.hermes_bin!s}")
print(f"HOST={c.server.host!s}")
print(f"PORT={c.server.port:d}")
print(f"SID={c.strategy_id!s}")
print(f"SYM={c.instrument.symbol!s}")
print(f"TF={c.instrument.timeframe!s}")
print(f"ACCT={c.execution.account!s}")
print(f"LIVE={'1' if c.execution.allow_live else '0'}")
PY
)"

# Health check always talks to loopback even when serving on 0.0.0.0.
HEALTH_HOST="$HOST"; [[ "$HEALTH_HOST" == "0.0.0.0" ]] && HEALTH_HOST="127.0.0.1"
BASE_URL="http://$HEALTH_HOST:$PORT"

say "Config:   agent=$CLIENT  instrument=$SYM $TF  account=$ACCT  strategy_id=$SID"

# --------------------------------------------------------------------------- #
# 3. Validate the decision brain and choose the serve command.                 #
# --------------------------------------------------------------------------- #
SERVE_BIN="$VENV/bin/hermes-bridge"
SERVE_ENV=()

if [[ "$CLIENT" == "hermes" ]]; then
  if [[ "$HMODE" == "cli" ]]; then
    [[ -x "$HBIN" ]] || die "hermes binary not executable at: $HBIN
   Fix agent.hermes.hermes_bin in config/trading.yaml, or run with --mock."
    ok "Brain: Hermes oneshot CLI (hermes -z) → $HBIN"
    if [[ "$CHECK_HERMES" == "1" ]]; then
      say "Pinging Hermes (this makes one real model call)…"
      if "$HBIN" -z "Reply with the single word: pong" >/dev/null 2>&1; then
        ok "Hermes responded."
      else
        warn "Hermes ping failed — the bridge will fall back to WAIT on errors."
      fi
    fi
  else
    # in_process: the bridge must import run_agent from the Hermes venv.
    HVENV="$HERMES_REPO/venv"
    [[ -d "$HVENV" ]] || die "Hermes venv not found at $HVENV (mode: in_process).
   Install Hermes (scripts/install_hermes.sh --install-hermes), set mode: cli, or use --mock."
    if [[ ! -x "$HVENV/bin/hermes-bridge" ]]; then
      say "Installing hermes-bridge into the Hermes venv…"
      uv pip install --python "$HVENV" -e "$BRIDGE_DIR"
    fi
    SERVE_BIN="$HVENV/bin/hermes-bridge"
    SERVE_ENV=(env "PYTHONPATH=$HERMES_REPO${PYTHONPATH:+:$PYTHONPATH}")
    ok "Brain: Hermes in-process (AIAgent) via $HVENV"
  fi
else
  ok "Brain: deterministic mock rules (no LLM)."
fi

if [[ "$LIVE" == "1" ]]; then
  warn "execution.allow_live is TRUE — this can place REAL orders. Ctrl-C now if unintended."
fi

# --------------------------------------------------------------------------- #
# 4. Print the connection banner once healthy (background), then BECOME the     #
#    server via exec so Ctrl-C / SIGTERM shut uvicorn down natively & cleanly.  #
# --------------------------------------------------------------------------- #
print_banner_when_ready() {
  # Wait for /health (or give up); only print the "up" banner if it truly answers.
  for _ in $(seq 1 60); do
    curl -fsS "$BASE_URL/health" >/dev/null 2>&1 && break
    sleep 0.25
  done
  if ! curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
    warn "Bridge did not answer /health at $BASE_URL — check the logs above."
    return 0
  fi

  # LAN IPs the Windows/NinjaTrader side can reach (server listens on 0.0.0.0).
  local lan="" ifc ip
  for ifc in en0 en1; do ip="$(ipconfig getifaddr "$ifc" 2>/dev/null || true)"; [[ -n "$ip" ]] && lan+="$ip "; done

  echo
  ok "Bridge is healthy."
  printf '\033[1;37m  ── Hermes bridge is up ─────────────────────────────────────\033[0m\n'
  echo "     Dashboard       $BASE_URL/"
  echo "     Health          $BASE_URL/health"
  echo "     Session status  $BASE_URL/session/status"
  echo
  echo "  NinjaTrader (Sim) → set on HermesBridgeStrategy:"
  echo "     BridgePort  = $PORT"
  echo "     StrategyId  = $SID            (must match exactly)"
  echo "     Account     = $ACCT          AllowLive = false"
  if [[ -n "$lan" ]]; then
    echo "     BridgeHost  = ${lan%% *}   (this Mac, from Parallels/Windows; or 127.0.0.1 if same host)"
  else
    echo "     BridgeHost  = <this Mac's LAN IP>   (or 127.0.0.1 if NinjaTrader runs on the Mac)"
  fi
  echo
  echo "  Kill switch (flatten + halt):  curl -X POST $BASE_URL/control/flatten"
  echo "  Resume after halt:             curl -X POST $BASE_URL/control/resume"
  echo "  Stop the bridge:               Ctrl-C"
  echo
  say "Streaming bridge logs — Ctrl-C to stop."
  echo
}

say "Starting bridge on $HOST:$PORT …"
print_banner_when_ready &   # prints the banner as soon as the server is up
# exec → this shell process becomes uvicorn; signals (Ctrl-C/TERM) hit it directly.
exec "${SERVE_ENV[@]}" "$SERVE_BIN" serve --config "$CONFIG" "${PASSTHRU[@]}"
