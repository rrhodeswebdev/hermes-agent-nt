"""Unknown-key guard: warn-at-runtime, strict-at-test-time (spec A7)."""

from pathlib import Path

from hermes_bridge.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_unknown_keys_detected_nested(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text(
        "execution:\n  armed_plans: true\n  account: SimX\nnot_a_section: 1\n",
        encoding="utf-8")
    cfg = load_config(p)
    unknown = [w for w in cfg.config_warnings if w.startswith("unknown key:")]
    assert "unknown key: execution.armed_plans" in unknown
    assert "unknown key: not_a_section" in unknown
    assert cfg.execution.account == "SimX"  # known keys still load


def test_clean_config_has_no_unknown_key_warnings(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text("risk:\n  max_contracts: 3\n", encoding="utf-8")
    cfg = load_config(p)
    assert [w for w in cfg.config_warnings if w.startswith("unknown key:")] == []


def test_committed_template_is_strictly_clean():
    """The committed template (deep-merged with trading.local.yaml when present —
    i.e. on the operator's machine during the mandatory pre-commit suite run) must
    contain zero unknown keys. This is the test-time 'forbid' half of spec A7."""
    cfg = load_config(REPO_ROOT / "config" / "trading.yaml")
    unknown = [w for w in cfg.config_warnings if w.startswith("unknown key:")]
    assert unknown == [], f"unknown config keys: {unknown}"


def test_distilled_vs_lessons_limit_warning(tmp_path):
    p = tmp_path / "trading.yaml"
    p.write_text(
        "learning:\n  distilled_char_limit: 9000\n  lessons_char_limit: 2500\n",
        encoding="utf-8")
    cfg = load_config(p)
    assert any("distilled_char_limit" in w for w in cfg.config_warnings)
