import json

from hermes_bridge.agent_client import AgentRequest, build_user_prompt, load_context_files
from hermes_bridge.config import BridgeConfig
from hermes_bridge.indicators import build_context
from hermes_bridge.session import SessionState
from tests.conftest import synthetic_bars


def test_load_context_files_missing_dir_returns_empty():
    assert load_context_files("does/not/exist") == ""


def test_load_context_files_reads_md(tmp_path):
    (tmp_path / "strategy.md").write_text("STRAT-BODY")
    (tmp_path / "extra.md").write_text("EXTRA-BODY")
    out = load_context_files(str(tmp_path))
    assert "STRAT-BODY" in out
    assert "EXTRA-BODY" in out


def test_load_context_files_handles_utf8(tmp_path):
    # Context files are UTF-8 (em-dash, arrows, etc.); reading must not depend on the
    # platform locale (Windows defaults to cp1252 and would crash without encoding).
    (tmp_path / "strategy.md").write_text("EM—DASH arrow → ok ✅", encoding="utf-8")
    out = load_context_files(str(tmp_path))
    assert "EM—DASH" in out
    assert "✅" in out


def test_build_user_prompt_shape():
    cfg = BridgeConfig()
    bars = synthetic_bars(120)
    ctx = build_context(bars, ema_fast=cfg.strategy.ema_fast,
                        ema_slow=cfg.strategy.ema_slow, atr_period=cfg.strategy.atr_period)
    sess = SessionState(cfg.instrument.symbol, cfg.instrument.timeframe,
                        cfg.instrument.tick_size, cfg.instrument.tick_value,
                        cfg.daily_goal.profit_target, cfg.daily_goal.max_daily_loss)
    req = AgentRequest(mode="seek_entry", context=ctx, recent_bars=bars,
                       account=sess.account_state(mark_price=bars[-1].close))
    prompt = build_user_prompt(req)
    assert prompt.startswith("CURRENT MARKET STATE:")
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["mode"] == "seek_entry"
    assert len(payload["recent_bars"]) <= 30
