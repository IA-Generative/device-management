import importlib
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient
from fastapi.responses import Response

# Minimal config fixture for filesystem fallback
_TEST_CONFIG = {
    "configVersion": 1,
    "config": {
        "authHeaderName": "Authorization",
        "authHeaderPrefix": "Bearer ",
        "enabled": True,
        "telemetryEnabled": True,
        "telemetrylogJson": True,
    },
}

_config_dir = None


def _ensure_config_fixture():
    global _config_dir
    if _config_dir and os.path.isdir(_config_dir):
        return _config_dir
    _config_dir = tempfile.mkdtemp(prefix="dm-test-config-")
    lo_dir = os.path.join(_config_dir, "libreoffice")
    os.makedirs(lo_dir, exist_ok=True)
    for d in [_config_dir, lo_dir]:
        for name in ("config.json", "config.prod.json"):
            with open(os.path.join(d, name), "w") as f:
                json.dump(_TEST_CONFIG, f)
    return _config_dir


def _load_module():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    cfg_dir = _ensure_config_fixture()

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_CONFIG_DIR"] = cfg_dir
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_TOKEN_SIGNING_KEY"] = "unit-test-signing-key"
    os.environ["DM_TELEMETRY_REQUIRE_TOKEN"] = "true"
    os.environ["DM_TELEMETRY_PUBLIC_ENDPOINT"] = "/telemetry/v1/traces"
    os.environ["PUBLIC_BASE_URL"] = "https://example.test/bootstrap"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATABASE_ADMIN_URL", None)

    sys.modules.pop("psycopg2", None)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def test_config_exposes_public_telemetry_settings_and_uses_token_endpoint_for_key():
    mod = _load_module()
    client = TestClient(mod.app)

    res = client.get("/config/libreoffice/config.json?profile=prod")
    assert res.status_code == 200
    body = res.json()
    cfg = body.get("config", {})

    assert cfg.get("telemetryEnabled") is True
    assert cfg.get("telemetryAuthorizationType") == "Bearer"
    assert cfg.get("telemetryEndpoint") == "https://example.test/telemetry/v1/traces"
    # telemetryKey is treated as a secret and is scrubbed unless relay auth is provided.
    assert cfg.get("telemetryKey", "") == ""
    assert int(cfg.get("telemetryKeyTtlSeconds")) > 0

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    assert token_res.status_code == 200
    token = token_res.json().get("telemetryKey")
    assert isinstance(token, str) and token


def test_telemetry_relay_rejects_missing_token():
    mod = _load_module()
    client = TestClient(mod.app)

    res = client.post(
        "/telemetry/v1/traces",
        data=b'{"resourceSpans":[]}',
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 401


def test_telemetry_relay_accepts_valid_token(monkeypatch):
    mod = _load_module()
    client = TestClient(mod.app)

    def _fake_forward(body: bytes, *, content_type: str, user_agent: str | None):
        assert body == b"test-payload"
        assert content_type == "application/x-protobuf"
        return Response(content=b"ok", status_code=202, headers={"Content-Type": "text/plain"})

    monkeypatch.setattr(mod, "_forward_telemetry_to_upstream", _fake_forward)

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    assert token_res.status_code == 200
    token = token_res.json().get("telemetryKey")
    assert token

    res = client.post(
        "/telemetry/v1/traces",
        data=b"test-payload",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert res.status_code == 202
    assert res.text == "ok"
