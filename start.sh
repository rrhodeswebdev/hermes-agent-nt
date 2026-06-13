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
#   ./start.sh --check-claude  # also do a live `claude -p` ping before serving
#   HERMES_BRIDGE_PORT=9000 ./start.sh   # env overrides are honored
#
# Ctrl-C cleanly stops the bridge.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$REPO_ROOT/bridge"
CONFIG="$REPO_ROOT/config/trading.yaml"
VENV="$BRIDGE_DIR/.venv"

FORCE_MOCK=0
CHECK_CLAUDE=0
PASSTHRU=()
for arg in "$@"; do
  case "$arg" in
    --mock)         FORCE_MOCK=1 ;;
    --check-claude) CHECK_CLAUDE=1 ;;
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
eval "$("$BRIDGE_DIR/.venv/bin/hermes-bridge" config-dump --config "$CONFIG")"

# Health check always talks to loopback even when serving on 0.0.0.0.
HEALTH_HOST="$HOST"; [[ "$HEALTH_HOST" == "0.0.0.0" ]] && HEALTH_HOST="127.0.0.1"
BASE_URL="http://$HEALTH_HOST:$PORT"

say "Config:   agent=$CLIENT  instrument=$SYM $TF  account=$ACCT  strategy_id=$SID"

# --------------------------------------------------------------------------- #
# 3. Validate the decision brain and choose the serve command.                 #
# --------------------------------------------------------------------------- #
SERVE_BIN="$VENV/bin/hermes-bridge"
SERVE_ENV=()

if [[ "$CLIENT" == "claude" ]]; then
  CBIN_PATH="$(command -v "$CBIN" 2>/dev/null || true)"
  [[ -n "$CBIN_PATH" ]] || die "claude CLI not found on PATH (agent.claude.claude_bin=$CBIN).
   Install Claude Code, fix agent.claude.claude_bin in config/trading.yaml, or run with --mock."
  ok "Brain: Claude CLI (claude -p, model=$CMODEL) → $CBIN_PATH"
  if [[ "$CHECK_CLAUDE" == "1" ]]; then
    say "Pinging Claude through the bridge's real call path (one model call)…"
    # `check` uses the same argv/schema/env as live decisions, so a pass is meaningful.
    if "$SERVE_BIN" check --config "$CONFIG"; then
      ok "Claude responded."
    else
      warn "Claude ping failed — the bridge will fall back to WAIT on errors."
    fi
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
