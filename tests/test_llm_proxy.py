"""Proxy LLM /llm/v1 — auth duale, passthrough (dont SSE), quota 429, métriques.

Couvre les critères d'acceptation 1, 2, 5 et 10 du ticket relais LLM.
"""
import os

from fastapi.testclient import TestClient

from tests.llm_utils import BACKEND_TOKEN, BackendRecorder, enroll, load_module


def _setup(extra_env=None):
    mod = load_module(extra_env)
    recorder = BackendRecorder()
    recorder.install()
    return mod, TestClient(mod.app), recorder


# ── Critère 1 : /models — 200 avec credentials valides, 401 sinon ────────────

def test_models_requires_credentials():
    _, client, recorder = _setup()
    res = client.get("/llm/v1/models")
    assert res.status_code == 401
    body = res.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "invalid_api_key"
    assert recorder.requests == []  # jamais forwardé sans auth


def test_models_with_valid_relay_headers():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    res = client.get("/llm/v1/models", headers=relay_headers)
    assert res.status_code == 200
    assert res.json()["data"][0]["id"] == "test-model"
    # La clé backend est injectée côté serveur…
    outbound = recorder.requests[-1]
    assert outbound.headers["authorization"] == f"Bearer {BACKEND_TOKEN}"
    # …et les credentials relay entrants ne sont PAS forwardés au backend.
    assert "x-relay-client" not in outbound.headers
    assert "x-relay-key" not in outbound.headers


def test_models_with_invalid_relay_key_is_401():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    relay_headers["X-Relay-Key"] = "invalid-key"
    res = client.get("/llm/v1/models", headers=relay_headers)
    assert res.status_code == 401
    assert recorder.requests == []


def test_legacy_credentials_with_config_target_allow_llm():
    """Rétrocompat : un client enrôlé AVANT l'ajout du target llm passe sans ré-enrôlement."""
    mod, client, _ = _setup()
    relay_headers = enroll(client)
    for row in mod._RELAY_MEMORY_STORE.values():
        row["allowed_targets"] = ["config", "keycloak"]  # credential historique
    res = client.get("/llm/v1/models", headers=relay_headers)
    assert res.status_code == 200


# ── Auth duale : llmToken signé lié au relay client ──────────────────────────

def test_models_with_minted_llm_token():
    mod, client, _ = _setup()
    enroll(client)  # peuple le store mémoire (re-check de révocation)
    from app.llm.tokens import mint_llm_token
    token, expires_at = mint_llm_token(
        client_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a", email="user@example.com"
    )
    assert token and expires_at
    res = client.get("/llm/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


def test_tampered_llm_token_is_401():
    _, client, recorder = _setup()
    from app.llm.tokens import mint_llm_token
    token, _ = mint_llm_token(client_uuid="x", email="user@example.com")
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    res = client.get("/llm/v1/models", headers={"Authorization": f"Bearer {tampered}"})
    assert res.status_code == 401
    assert recorder.requests == []


def test_revoked_client_llm_token_is_401():
    mod, client, _ = _setup()
    relay_headers = enroll(client)
    res = client.get(
        "/config/libreoffice/config.json?profile=prod",
        headers={**relay_headers, "X-Plugin-Version": "1.0.0"},
    )
    token = res.json()["config"]["llmToken"]
    assert token
    for row in mod._RELAY_MEMORY_STORE.values():
        row["revoked"] = True
    denied = client.get("/llm/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 401


# ── Critère 2 : chat/completions, dont passthrough SSE ───────────────────────

def test_chat_completions_non_stream():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    res = client.post(
        "/llm/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "salut"}]},
        headers=relay_headers,
    )
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "Bonjour"
    # Modèle par défaut injecté quand absent (DEFAULT_MODEL_NAME).
    import json as _json
    outbound_payload = _json.loads(recorder.requests[-1].content)
    assert outbound_payload["model"] == "test-model"
    # La clé backend n'apparaît jamais côté client.
    assert BACKEND_TOKEN not in res.text
    assert BACKEND_TOKEN not in str(res.headers)


def test_embeddings_requires_credentials():
    _, client, recorder = _setup()
    res = client.post("/llm/v1/embeddings", json={"model": "m", "input": "salut"})
    assert res.status_code == 401
    assert recorder.requests == []  # jamais forwardé sans auth


def test_embeddings_passthrough_with_relay_auth():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    res = client.post(
        "/llm/v1/embeddings",
        json={"model": "bge-multilingual-gemma2", "input": "salut"},
        headers=relay_headers,
    )
    assert res.status_code == 200
    # Le vecteur d'embeddings traverse tel quel.
    assert res.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    # Le backend a bien reçu la requête SUR /embeddings, clé backend injectée côté serveur.
    outbound = recorder.requests[-1]
    assert outbound.url.path.endswith("/embeddings")
    assert outbound.headers["authorization"] == f"Bearer {BACKEND_TOKEN}"
    # Les credentials relay ne fuient pas au backend, ni la clé côté client.
    assert "x-relay-key" not in outbound.headers
    assert BACKEND_TOKEN not in res.text


def test_embeddings_counts_toward_quota():
    _, client, _ = _setup({"LLM_QUOTA_REQUESTS_PER_MINUTE": "2", "LLM_QUOTA_WINDOW_SECONDS": "60"})
    relay_headers = enroll(client)
    payload = {"model": "m", "input": "x"}
    assert client.post("/llm/v1/embeddings", json=payload, headers=relay_headers).status_code == 200
    assert client.post("/llm/v1/embeddings", json=payload, headers=relay_headers).status_code == 200
    res = client.post("/llm/v1/embeddings", json=payload, headers=relay_headers)
    assert res.status_code == 429  # throttling par utilisateur s'applique aussi aux embeddings


def test_chat_completions_stream_passthrough():
    _, client, _ = _setup()
    relay_headers = enroll(client)
    chunks = []
    with client.stream(
        "POST",
        "/llm/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "salut"}]},
        headers=relay_headers,
    ) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        # Vraie réponse streamée : pas de Content-Length (pas de bufferisation
        # de la réponse complète). Le forwarding chunk par chunk est prouvé
        # unitairement dans test_llm_pipeline (wrap_stream).
        assert "content-length" not in res.headers
        assert res.headers["cache-control"] == "no-store"
        for chunk in res.iter_raw():
            if chunk:
                chunks.append(chunk)
    body = b"".join(chunks)
    assert body.count(b"data:") == 3
    assert body.endswith(b"data: [DONE]\n\n")
    assert BACKEND_TOKEN.encode() not in body


# ── Critère 5 : quota simulé → 429 propre {error, retry_after} ───────────────

def test_quota_exceeded_returns_429_with_retry_after():
    _, client, _ = _setup()
    relay_headers = enroll(client)
    os.environ["LLM_QUOTA_REQUESTS_PER_MINUTE"] = "2"  # hot-reload : relu par requête
    # Fenêtre large : évite qu'un changement de fenêtre entre deux requêtes
    # (frontière des 60 s) remette le compteur à zéro en plein test (flaky CI).
    os.environ["LLM_QUOTA_WINDOW_SECONDS"] = "3600"
    payload = {"messages": [{"role": "user", "content": "salut"}]}
    assert client.post("/llm/v1/chat/completions", json=payload, headers=relay_headers).status_code == 200
    assert client.post("/llm/v1/chat/completions", json=payload, headers=relay_headers).status_code == 200
    res = client.post("/llm/v1/chat/completions", json=payload, headers=relay_headers)
    assert res.status_code == 429
    body = res.json()
    assert body["error"]["type"] == "rate_limit_exceeded"
    assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
    assert res.headers["Retry-After"] == str(body["retry_after"])


# ── Critère 10 : trace-id propagé + /metrics alimenté ────────────────────────

def test_trace_id_echoed_end_to_end():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    res = client.get(
        "/llm/v1/models", headers={**relay_headers, "X-Request-Id": "trace-abc123"}
    )
    assert res.headers["X-Request-Id"] == "trace-abc123"
    # …et propagé au backend.
    assert recorder.requests[-1].headers["x-request-id"] == "trace-abc123"


def test_metrics_expose_llm_histogram_after_traffic():
    _, client, _ = _setup()
    relay_headers = enroll(client)
    client.get("/llm/v1/models", headers=relay_headers)
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "dm_llm_request_duration_seconds_bucket" in metrics.text
    assert "dm_llm_requests_total" in metrics.text
    assert BACKEND_TOKEN not in metrics.text
