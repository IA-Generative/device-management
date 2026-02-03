import importlib
import os
import sys

from fastapi.testclient import TestClient


def _load_app():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    os.environ.setdefault("DM_CONFIG_ENABLED", "true")
    os.environ.setdefault("DM_CONFIG_PROFILE", "dev")
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod.app


def test_enroll_valid_payload():
    app = _load_app()
    client = TestClient(app)
    payload = {
        "device_name": "matisse",
        "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
        "email": "user@example.com",
    }
    res = client.post("/enroll", json=payload)
    assert res.status_code == 201
    assert res.json().get("ok") is True


def test_enroll_missing_fields():
    app = _load_app()
    client = TestClient(app)
    payload = {"device_name": "", "plugin_uuid": " ", "email": ""}
    res = client.post("/enroll", json=payload)
    assert res.status_code == 400
    body = res.json()
    assert body.get("ok") is False
    # Error message format changed with Pydantic validation
    assert "device_name" in body.get("error", "") or "Missing" in body.get("error", "")
