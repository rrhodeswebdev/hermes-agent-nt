import json

from hermes_bridge.agent_client import build_user_prompt, load_context_files
from hermes_bridge.config import BridgeConfig
from tests.conftest import make_agent_request


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
    prompt = build_user_prompt(make_agent_request(BridgeConfig()))
    assert prompt.startswith("CURRENT MARKET STATE:")
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["mode"] == "seek_entry"
    assert len(payload["recent_bars"]) <= 30
