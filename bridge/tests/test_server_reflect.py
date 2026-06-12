from fastapi.testclient import TestClient

from hermes_bridge.config import BridgeConfig
from hermes_bridge.server import create_app


def _client(tmp_path):
    cfg = BridgeConfig()
    cfg.learning.learned_dir = str(tmp_path / "learned")
    cfg.learning.journal_path = str(tmp_path / "j.jsonl")
    cfg.learning.reflect_enabled = False
    return TestClient(create_app(cfg))


def test_app_builds_with_reflector():
    app = create_app(BridgeConfig())
    assert app.state.appstate.reflector is not None


def test_control_curate_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_bridge.reflect.Reflector.curate",
                        lambda self: {"lessons": 0, "notes": 0, "profile": 0})
    c = _client(tmp_path)
    r = c.post("/control/curate")
    assert r.status_code == 200
    assert "applied" in r.json()
