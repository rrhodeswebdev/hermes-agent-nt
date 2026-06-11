import json
import types
from pathlib import Path

from hermes_bridge.agent_client import AgentRequest
from hermes_bridge.claude_agent import ClaudeAgentClient
from hermes_bridge.config import BridgeConfig
from hermes_bridge.indicators import build_context
from hermes_bridge.journal import ClosedTrade, JournalStore
from hermes_bridge.models import Action
from hermes_bridge.session import SessionState
from tests.conftest import synthetic_bars


def _req(cfg):
    bars = synthetic_bars(120)
    ctx = build_context(bars, ema_fast=cfg.strategy.ema_fast,
                        ema_slow=cfg.strategy.ema_slow, atr_period=cfg.strategy.atr_period)
    sess = SessionState(cfg.instrument.symbol, cfg.instrument.timeframe,
                        cfg.instrument.tick_size, cfg.instrument.tick_value,
                        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss)
    return AgentRequest(mode="seek_entry", context=ctx, recent_bars=bars,
                        account=sess.account_state(mark_price=bars[-1].close)), ctx


def test_decision_prompt_includes_learned_and_past_trades(tmp_path, monkeypatch):
    learned = tmp_path / "learned"
    (learned / "lessons").mkdir(parents=True)
    (learned / "trader-profile.md").write_text("PROFILE-MARKER risk-averse.", encoding="utf-8")
    (learned / "lessons" / "l1.md").write_text(
        "---\nname: lesson-marker\nstatus: active\n---\nLESSON-BODY-MARKER", encoding="utf-8")

    cfg = BridgeConfig()
    cfg.agent.client = "claude"
    cfg.learning.learned_dir = str(learned)
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    cfg.agent.claude.context_dir = "does/not/exist"  # force context_hint, keep prompt small

    req, ctx = _req(cfg)
    js = JournalStore(cfg.learning.journal_path)
    js.append(ClosedTrade(entry_ts=1, exit_ts=2, side="LONG", qty=1, entry_price=1,
                          exit_price=2, realized_pnl=7.0, bars_held=3, mae=-1, mfe=3,
                          trend=ctx.trend, entry_context={"trend": ctx.trend},
                          rationale="PASTTRADE-MARKER"))

    captured = {}

    def fake_run(cmd, **kwargs):
        i = cmd.index("--system-prompt-file")
        captured["system"] = Path(cmd[i + 1]).read_text(encoding="utf-8")
        captured["input"] = kwargs.get("input")
        return types.SimpleNamespace(
            stdout=json.dumps({"is_error": False, "structured_output": {"action": "WAIT"}}),
            stderr="", returncode=0)

    monkeypatch.setattr("hermes_bridge.claude_cli.subprocess.run", fake_run)
    d = ClaudeAgentClient(cfg).decide(req)
    assert d.action is Action.WAIT
    assert "PROFILE-MARKER" in captured["system"]
    assert "LESSON-BODY-MARKER" in captured["system"]
    assert "PASTTRADE-MARKER" in captured["input"]
