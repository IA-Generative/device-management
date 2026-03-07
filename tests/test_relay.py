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


def _load_module():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_RELAY_SECRET_PEPPER"] = "unit-test-pepper"
    os.environ["DM_RELAY_PROXY_SHARED_TOKEN"] = "proxy-shared-token"
    os.environ["DM_RELAY_ALLOWED_TARGETS_CSV"] = "keycloak,config,llm,mcr-api,telemetry"
    os.environ["DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["KEYCLOAK_ISSUER_URL"] = "https://issuer.from.config.test/realms/bootstrap"
    os.environ["PUBLIC_BASE_URL"] = "https://example.test/bootstrap"
    os.environ["LLM_API_TOKEN"] = "very-secret-token"

    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def _enroll_and_get_relay_headers(client: TestClient):
    token = _mk_fake_jwt({"email": "user@example.com", "exp": 4102444800})
    payload = {
        "device_name": "libreoffice",
        "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
    }
    res = client.post("/enroll", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 201
    body = res.json()
    return {
        "X-Relay-Client": body["relayClientId"],
        "X-Relay-Key": body["relayClientKey"],
    }


def test_config_hides_secrets_without_relay_key():
    mod = _load_module()
    client = TestClient(mod.app)

    res = client.get("/config/libreoffice/config.json?profile=prod")
    assert res.status_code == 200
    cfg = res.json().get("config", {})

    assert cfg.get("llm_api_tokens", "") == ""
    assert cfg.get("keycloakIssuerUrl") == "https://issuer.from.config.test/realms/bootstrap"


def test_config_returns_secrets_with_valid_relay_key():
    mod = _load_module()
    client = TestClient(mod.app)
    relay_headers = _enroll_and_get_relay_headers(client)

    res = client.get("/config/libreoffice/config.json?profile=prod", headers=relay_headers)
    assert res.status_code == 200
    cfg = res.json().get("config", {})

    assert cfg.get("llm_api_tokens") == "very-secret-token"
    assert cfg.get("relayAssistantBaseUrl") == "https://example.test/bootstrap/relay-assistant"


def test_relay_authorize_requires_proxy_shared_token():
    mod = _load_module()
    client = TestClient(mod.app)
    relay_headers = _enroll_and_get_relay_headers(client)

    denied = client.get("/relay/authorize?target=keycloak", headers=relay_headers)
    assert denied.status_code == 403

    ok_headers = dict(relay_headers)
    ok_headers["X-Relay-Proxy-Token"] = "proxy-shared-token"
    allowed = client.get("/relay/authorize?target=keycloak", headers=ok_headers)
    assert allowed.status_code == 200
    assert allowed.json().get("ok") is True


def test_relay_authorize_telemetry_with_proxy_token():
    mod = _load_module()
    client = TestClient(mod.app)
    relay_headers = _enroll_and_get_relay_headers(client)

    ok_headers = dict(relay_headers)
    ok_headers["X-Relay-Proxy-Token"] = "proxy-shared-token"
    allowed = client.get("/relay/authorize?target=telemetry", headers=ok_headers)
    assert allowed.status_code == 200
    body = allowed.json()
    assert body.get("ok") is True
    assert body.get("target") == "telemetry"
