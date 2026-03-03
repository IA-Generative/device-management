import importlib
import os
import sys

from fastapi.testclient import TestClient
from fastapi.responses import Response


def _load_module():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    os.environ.setdefault("DM_CONFIG_ENABLED", "true")
    os.environ.setdefault("DM_CONFIG_PROFILE", "prod")
    os.environ.setdefault("DM_TELEMETRY_ENABLED", "true")
    os.environ.setdefault("DM_TELEMETRY_TOKEN_SIGNING_KEY", "unit-test-signing-key")
    os.environ.setdefault("DM_TELEMETRY_REQUIRE_TOKEN", "true")
    os.environ.setdefault("DM_TELEMETRY_PUBLIC_ENDPOINT", "/telemetry/v1/traces")
    os.environ.setdefault("PUBLIC_BASE_URL", "https://example.test/bootstrap")

    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def test_config_injects_rotating_telemetry_token():
    mod = _load_module()
    client = TestClient(mod.app)

    res = client.get("/config/libreoffice/config.json?profile=prod")
    assert res.status_code == 200
    body = res.json()
    cfg = body.get("config", {})

    assert cfg.get("telemetryEnabled") is True
    assert cfg.get("telemetryAuthorizationType") == "Bearer"
    assert cfg.get("telemetryEndpoint") == "https://example.test/telemetry/v1/traces"
    assert isinstance(cfg.get("telemetryKey"), str)
    assert cfg.get("telemetryKey")
    assert int(cfg.get("telemetryKeyTtlSeconds")) > 0


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
