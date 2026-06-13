# Hermes × NinjaTrader 8 — developer tasks
# The Python bridge lives in bridge/ with its own uv-managed venv.

BRIDGE   := bridge
VENV     := $(BRIDGE)/.venv
PY       := $(VENV)/bin/python
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff
CLI      := $(VENV)/bin/hermes-bridge
CONFIG   := config/trading.yaml

.PHONY: help start setup test lint replay serve sample clean

help:
	@echo "Targets:"
	@echo "  start    one command to bring up everything (Mac side): ./start.sh"
	@echo "  setup    create the bridge venv (Python 3.11) and install deps"
	@echo "  test     run the bridge test suite"
	@echo "  lint     run ruff over the bridge package"
	@echo "  replay   run the synthetic-bar replay demo (no LLM, no NinjaTrader)"
	@echo "  serve    start the bridge HTTP server"
	@echo "  sample   regenerate bridge/replay/sample_bars.csv"
	@echo "  clean    remove the venv and Python caches"

# One-command startup: reads config, sets up the venv if needed, picks the right
# serve path, waits for health, prints the NinjaTrader connection info, streams logs.
start:
	./start.sh

setup:
	cd $(BRIDGE) && uv venv --python 3.11 .venv && uv pip install --python .venv -e ".[dev]"

test:
	cd $(BRIDGE) && .venv/bin/pytest

lint:
	cd $(BRIDGE) && .venv/bin/ruff check .

replay:
	cd $(BRIDGE) && .venv/bin/hermes-bridge replay replay/sample_bars.csv -v --agent mock --config ../$(CONFIG)

serve:
	cd $(BRIDGE) && .venv/bin/hermes-bridge serve --config ../$(CONFIG)

sample:
	cd $(BRIDGE)/replay && ../.venv/bin/python gen_sample.py

clean:
	rm -rf $(VENV)
	find $(BRIDGE) -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(BRIDGE)/.pytest_cache $(BRIDGE)/.ruff_cache $(BRIDGE)/*.egg-info
