import base64
import importlib
import json
import os
import sys

from fastapi.testclient import TestClient


def _mk_fake_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    def _enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_enc(header)}.{_enc(payload)}.sig"


def _load_app(*, verify_access_token: bool = False):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "dev"
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_RELAY_SECRET_PEPPER"] = "unit-test-pepper"
    os.environ["DM_RELAY_ALLOWED_TARGETS_CSV"] = "keycloak,config,llm"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "true" if verify_access_token else "false"

    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod.app


def test_enroll_requires_pkce_access_token():
    app = _load_app()
    client = TestClient(app)
    payload = {
        "device_name": "libreoffice",
        "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
    }
    res = client.post("/enroll", json=payload)
    assert res.status_code == 401
    assert res.json().get("ok") is False


def test_enroll_returns_relay_credentials_after_pkce():
    app = _load_app()
    client = TestClient(app)

    token = _mk_fake_jwt({"email": "user@example.com", "exp": 4102444800})
    payload = {
        "device_name": "libreoffice",
        "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
    }
    res = client.post("/enroll", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 201
    body = res.json()
    assert body.get("ok") is True
    assert body.get("relayClientId")
    assert body.get("relayClientKey")
    assert isinstance(body.get("relayKeyExpiresAt"), int)


def test_enroll_requires_auth_backend_when_verification_enabled():
    app = _load_app(verify_access_token=True)
    client = TestClient(app)

    token = _mk_fake_jwt({"email": "user@example.com", "exp": 4102444800})
    payload = {
        "device_name": "libreoffice",
        "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
    }
    res = client.post("/enroll", json=payload, headers={"Authorization": f"Bearer {token}"})
    # In unit tests, no OIDC/JWKS backend is configured/reachable, so the API
    # must fail closed instead of accepting unsigned JWT payloads.
    assert res.status_code == 503


def test_enroll_missing_fields():
    app = _load_app()
    client = TestClient(app)
    token = _mk_fake_jwt({"email": "user@example.com", "exp": 4102444800})

    payload = {"device_name": "", "plugin_uuid": " "}
    res = client.post("/enroll", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 400
    body = res.json()
    assert body.get("ok") is False
    assert "Missing required fields" in body.get("error", "")
