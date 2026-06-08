#!/usr/bin/env bash
# Install/refresh the Hermes customization for this project, and (optionally) install
# the Hermes Agent runtime itself.
#
# By default this script ONLY copies our customization files into place. It will not
# run the official Hermes network installer unless you pass --install-hermes, so you
# stay in control of what touches your system.
#
# Usage:
#   scripts/install_hermes.sh                 # copy customization only
#   scripts/install_hermes.sh --install-hermes   # also run the official Hermes installer
#   HERMES_TOOLS_DIR=/path/to/hermes/tools scripts/install_hermes.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DO_INSTALL_HERMES=0
[[ "${1:-}" == "--install-hermes" ]] && DO_INSTALL_HERMES=1

echo "Repo:        $REPO_ROOT"
echo "Hermes home: $HERMES_HOME"

if [[ "$DO_INSTALL_HERMES" == "1" ]]; then
  if command -v hermes >/dev/null 2>&1; then
    echo "Hermes already installed: $(command -v hermes)"
  else
    echo "Running the official Hermes Agent installer (Nous Research)…"
    echo "  https://github.com/NousResearch/hermes-agent"
    # The official one-line installer (sets up uv, Python 3.11, clones the repo).
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  fi
else
  echo "Skipping Hermes runtime install (pass --install-hermes to run it)."
  command -v hermes >/dev/null 2>&1 && echo "Detected hermes: $(command -v hermes)" \
    || echo "NOTE: 'hermes' not found on PATH. Install it, then re-run with the agent enabled."
fi

echo
echo "Copying personality…"
mkdir -p "$HERMES_HOME/personalities"
cp "$REPO_ROOT/hermes/personalities/hermes-trader.md" "$HERMES_HOME/personalities/"

echo "Copying cron schedule…"
mkdir -p "$HERMES_HOME/cron"
cp "$REPO_ROOT/hermes/cron/trading-session.yaml" "$HERMES_HOME/cron/"

# Tools: try to find the Hermes install's tools/ dir; fall back to ~/.hermes/tools.
TOOLS_DIR="${HERMES_TOOLS_DIR:-}"
if [[ -z "$TOOLS_DIR" ]]; then
  if [[ -d "$HERMES_HOME/tools" ]]; then
    TOOLS_DIR="$HERMES_HOME/tools"
  else
    TOOLS_DIR="$HERMES_HOME/tools"
    mkdir -p "$TOOLS_DIR"
  fi
fi
echo "Copying nt_* tools into: $TOOLS_DIR"
cp "$REPO_ROOT/hermes/tools/ninjatrader.py" "$TOOLS_DIR/"

echo "Context files live in: $REPO_ROOT/hermes/context (point your Hermes project context here)."
echo
echo "Done. Next:"
echo "  1) Ensure the 'ninjatrader' toolset is enabled for your agent."
echo "  2) Set HERMES_BRIDGE_URL / HERMES_STRATEGY_ID (see hermes/README.md)."
echo "  3) Start the bridge: scripts/run_bridge.sh"
