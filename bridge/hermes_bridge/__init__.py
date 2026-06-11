"""Hermes Bridge — connects NinjaTrader 8 to the Hermes trading agent.

The bridge is the single authority that can emit orders to NinjaTrader: every
command (from the deterministic engine, the LLM agent's tools, or a manual call)
passes through the server-side RiskGate before it is queued for execution.
"""

__version__ = "0.1.0"
