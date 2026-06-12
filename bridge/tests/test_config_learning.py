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
