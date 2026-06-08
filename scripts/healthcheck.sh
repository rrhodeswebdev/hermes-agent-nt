#!/usr/bin/env bash
# Quick liveness + session snapshot of the running bridge.
set -euo pipefail
URL="${HERMES_BRIDGE_URL:-http://127.0.0.1:8787}"

echo "GET $URL/health"
curl -fsS "$URL/health" && echo
echo "GET $URL/session/status"
curl -fsS "$URL/session/status" && echo
