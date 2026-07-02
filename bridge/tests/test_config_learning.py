from hermes_bridge.config import BridgeConfig, load_config


def test_learning_defaults():
    c = BridgeConfig()
    assert c.learning.enabled is True
    assert c.learning.learned_dir == "hermes/learned"
    assert c.learning.journal_path == "bridge/state/journal.jsonl"
    assert c.learning.retrieve_k == 3


def test_learning_from_yaml(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("learning:\n  retrieve_k: 5\n  enabled: false\n")
    c = load_config(str(p))
    assert c.learning.retrieve_k == 5
    assert c.learning.enabled is False


def test_consolidate_cadence_defaults_are_neutral():
    lc = BridgeConfig().learning
    assert lc.consolidate_enabled is False          # OFF in the committed template
    assert lc.consolidate_interval_minutes == 120.0
    assert lc.consolidate_startup_delay_s == 90.0


def test_day_review_char_limit_default():
    from hermes_bridge.config import LearningConfig
    assert LearningConfig().day_review_char_limit == 4000
