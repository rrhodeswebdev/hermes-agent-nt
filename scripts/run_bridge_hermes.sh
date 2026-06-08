#!/usr/bin/env bash
# Run the bridge INSIDE the Hermes venv so it can import run_agent (AIAgent) in-process.
# Use this instead of run_bridge.sh when config has agent.client: hermes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_REPO="$HERMES_HOME/hermes-agent"
VENV="$HERMES_REPO/venv"

if [[ ! -d "$VENV" ]]; then
  echo "Hermes venv not found at $VENV — install Hermes first (scripts/install_hermes.sh)." >&2
  exit 1
fi

# Ensure the bridge is installed into the Hermes venv (deps are already satisfied there).
if [[ ! -x "$VENV/bin/hermes-bridge" ]]; then
  echo "Installing hermes-bridge into the Hermes venv…"
  uv pip install --python "$VENV" -e "$REPO_ROOT/bridge"
fi

# PYTHONPATH makes `import run_agent` resolve to the Hermes repo.
export PYTHONPATH="$HERMES_REPO${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV/bin/hermes-bridge" serve \
  --config "$REPO_ROOT/config/trading.yaml" --host 0.0.0.0 --port 8787 "$@"
