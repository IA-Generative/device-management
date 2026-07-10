"""Helpers partagés des tests du proxy LLM (/llm/v1).

Étend le pattern de test_relay.py (module app.main rechargé avec un env préparé,
enrôlement mémoire sans DB) avec : injection d'un backend LLM factice via
httpx.MockTransport (y compris flux SSE), et reset des singletons app.llm
(client httpx partagé, quota store) entre les tests.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import sys
import tempfile

import httpx
from fastapi.testclient import TestClient

_TEST_CONFIG = {
    "configVersion": 1,
    "config": {
        "llm_base_urls": "${{LLM_BASE_URL}}",
        "llm_api_tokens": "${{LLM_API_TOKEN}}",
        "authHeaderName": "Authorization",
        "authHeaderPrefix": "Bearer ",
        "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
        "enabled": True,
        "telemetryEnabled": True,
        "telemetrylogJson": True,
    },
}

BACKEND_URL = "https://backend.test/v1"
BACKEND_TOKEN = "very-secret-backend-token"  # nosec B105: valeur factice de test
PROXY_PUBLIC_URL = "https://example.test/bootstrap/llm/v1"

_config_dir = None


def ensure_config_fixture() -> str:
    global _config_dir
    if _config_dir and os.path.isdir(_config_dir):
        return _config_dir
    _config_dir = tempfile.mkdtemp(prefix="dm-test-llm-config-")
    lo_dir = os.path.join(_config_dir, "libreoffice")
    os.makedirs(lo_dir, exist_ok=True)
    for d in (_config_dir, lo_dir):
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump(_TEST_CONFIG, f)
        with open(os.path.join(d, "config.prod.json"), "w") as f:
            json.dump(_TEST_CONFIG, f)
    return _config_dir


def mk_fake_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def _enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_enc(header)}.{_enc(payload)}.sig"


def load_module(extra_env: dict[str, str] | None = None):
    """Recharge app.main avec l'env de test LLM (sans DB → store mémoire)."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_CONFIG_DIR"] = ensure_config_fixture()
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_RELAY_SECRET_PEPPER"] = "unit-test-pepper"
    os.environ["DM_RELAY_ALLOWED_TARGETS_CSV"] = "keycloak,config,llm,telemetry"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["KEYCLOAK_ISSUER_URL"] = "https://issuer.from.config.test/realms/bootstrap"
    os.environ["PUBLIC_BASE_URL"] = "https://example.test/bootstrap"
    os.environ["PUBLIC_LLM_PROXY_URL"] = PROXY_PUBLIC_URL
    os.environ["LLM_BASE_URL"] = BACKEND_URL
    os.environ["LLM_API_TOKEN"] = BACKEND_TOKEN
    os.environ["DEFAULT_MODEL_NAME"] = "test-model"
    os.environ["DM_LLM_TOKEN_SIGNING_KEY"] = "unit-test-llm-signing-key"
    # Clés hot-reload : repartir propre à chaque chargement.
    for key in ("LLM_QUOTA_REQUESTS_PER_MINUTE", "LLM_QUOTA_WINDOW_SECONDS",
                "LLM_GUARDRAILS", "LLM_BACKENDS", "FORCE_LLM_ENDPOINT_OVERRIDE",
                "DM_RUNTIME_MODE"):
        os.environ.pop(key, None)
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATABASE_ADMIN_URL", None)
    if extra_env:
        os.environ.update(extra_env)

    sys.modules.pop("psycopg2", None)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)

    # Reset des singletons app.llm (modules non rechargés avec app.main).
    from app.llm import http_client, throttle
    http_client.set_transport_for_tests(None)
    throttle.reset_quota_store_for_tests()
    return mod


def enroll(client: TestClient) -> dict[str, str]:
    token = mk_fake_jwt({"email": "user@example.com", "exp": 4102444800})
    res = client.post(
        "/enroll",
        json={"device_name": "libreoffice", "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    return {"X-Relay-Client": body["relayClientId"], "X-Relay-Key": body["relayClientKey"]}


class SSEStream(httpx.AsyncByteStream):
    """Flux de chunks contrôlé pour simuler un backend SSE."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


class BackendRecorder:
    """Backend LLM factice : enregistre les requêtes sortantes, répond selon le chemin."""

    def __init__(self, sse_chunks: list[bytes] | None = None):
        self.requests: list[httpx.Request] = []
        self.sse_chunks = sse_chunks or [
            b'data: {"choices":[{"delta":{"content":"Bon"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"jour"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "test-model"}]})
        if path.endswith("/chat/completions"):
            try:
                payload = json.loads(request.content or b"{}")
            except ValueError:
                payload = {}
            if payload.get("stream"):
                return httpx.Response(
                    200,
                    stream=SSEStream(self.sse_chunks),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": payload.get("model", ""),
                "choices": [{"message": {"role": "assistant", "content": "Bonjour"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            })
        return httpx.Response(404, json={"error": "unknown path"})

    def install(self) -> None:
        from app.llm import http_client
        http_client.set_transport_for_tests(httpx.MockTransport(self.handler))
