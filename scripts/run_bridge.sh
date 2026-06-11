#!/usr/bin/env bash
# Start the hermes-bridge server. Creates the venv on first run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/bridge"

if [[ ! -x ".venv/bin/hermes-bridge" ]]; then
  echo "Setting up the bridge venv (first run)…"
  uv venv --python 3.11 .venv
  uv pip install --python .venv -e ".[dev]"
fi

exec .venv/bin/hermes-bridge serve --config "$REPO_ROOT/config/trading.yaml" "$@"
