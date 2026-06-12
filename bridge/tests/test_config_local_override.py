"""config/trading.local.yaml deep-merges over the base config (personal values stay local)."""

from hermes_bridge.config import load_config


def test_local_yaml_overrides_base(tmp_path):
    base = tmp_path / "trading.yaml"
    base.write_text(
        "execution:\n  account: Sim101\n"
        "daily_goal:\n  profit_target: 500.0\n  max_daily_loss: 400.0\n",
        encoding="utf-8",
    )
    local = tmp_path / "trading.local.yaml"
    local.write_text(
        "daily_goal:\n  max_daily_loss: 250.0\n",
        encoding="utf-8",
    )

    cfg = load_config(base)

    # overridden by the local file
    assert cfg.daily_goal.max_daily_loss == 250.0
    # sibling field from the base survives the deep-merge (not clobbered)
    assert cfg.daily_goal.profit_target == 500.0
    # a section the local file doesn't touch stays as the base defined it
    assert cfg.execution.account == "Sim101"


def test_no_local_yaml_uses_base_only(tmp_path):
    base = tmp_path / "trading.yaml"
    base.write_text("execution:\n  account: Sim101\n", encoding="utf-8")

    cfg = load_config(base)

    assert cfg.execution.account == "Sim101"
