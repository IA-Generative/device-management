import base64
import importlib
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

# Minimal config fixture for tests — uses ${{VAR}} placeholders
# that get substituted by the env vars set in _load_module().
_TEST_CONFIG = {
    "configVersion": 1,
    "config": {
        "llm_base_urls": "${{LLM_BASE_URL}}",
        "llm_api_tokens": "${{LLM_API_TOKEN}}",
        "authHeaderName": "Authorization",
        "authHeaderPrefix": "Bearer ",
        "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
        "keycloakRealm": "${{KEYCLOAK_REALM}}",
        "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
        "enabled": True,
        "telemetryEnabled": True,
        "telemetrylogJson": True,
    },
}

# Temp config directory shared across tests in this module
_config_dir = None


def _ensure_config_fixture():
    """Create a temporary config directory with a minimal config.json for filesystem fallback."""
    global _config_dir
    if _config_dir and os.path.isdir(_config_dir):
        return _config_dir
    _config_dir = tempfile.mkdtemp(prefix="dm-test-config-")
    lo_dir = os.path.join(_config_dir, "libreoffice")
    os.makedirs(lo_dir, exist_ok=True)
    for d in [_config_dir, lo_dir]:
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump(_TEST_CONFIG, f)
        with open(os.path.join(d, "config.prod.json"), "w") as f:
            json.dump(_TEST_CONFIG, f)
    return _config_dir


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

    cfg_dir = _ensure_config_fixture()

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_CONFIG_DIR"] = cfg_dir
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_RELAY_SECRET_PEPPER"] = "unit-test-pepper"
    os.environ["DM_RELAY_PROXY_SHARED_TOKEN"] = "proxy-shared-token"
    os.environ["DM_RELAY_ALLOWED_TARGETS_CSV"] = "keycloak,config,llm,mcr-api,telemetry"
    os.environ["DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["KEYCLOAK_ISSUER_URL"] = "https://issuer.from.config.test/realms/bootstrap"
    os.environ["PUBLIC_BASE_URL"] = "https://example.test/bootstrap"
    os.environ["LLM_API_TOKEN"] = "very-secret-token"
    # Unset DATABASE_URL to prevent real DB connections (may be set by other test modules)
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATABASE_ADMIN_URL", None)

    # Remove any fake psycopg2 injected by other test modules
    sys.modules.pop("psycopg2", None)
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
