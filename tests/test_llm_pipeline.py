"""Pipeline enfichable du proxy LLM — guardrails par config, backend registry,
quota partagé multi-réplicas. Couvre les critères 7 (unitaire), 8 et 9.
"""
import json
import os
import time

from fastapi.testclient import TestClient

from tests.llm_utils import BackendRecorder, enroll, load_module


def _setup(extra_env=None):
    mod = load_module(extra_env)
    recorder = BackendRecorder()
    recorder.install()
    return mod, TestClient(mod.app), recorder


_PAYLOAD = {"messages": [{"role": "user", "content": "salut"}]}


# ── Critère 8 : guardrail branché/débranché PAR CONFIGURATION seulement ──────

def test_deny_all_guardrail_blocks_without_core_change():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    # Config par défaut : noop → passe.
    assert client.post("/llm/v1/chat/completions", json=_PAYLOAD, headers=relay_headers).status_code == 200
    # Kill-switch activé à chaud (même mécanique que l'onglet Config admin).
    os.environ["LLM_GUARDRAILS"] = "deny_all"
    calls_before = len(recorder.requests)
    res = client.post("/llm/v1/chat/completions", json=_PAYLOAD, headers=relay_headers)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "content_policy_violation"
    assert len(recorder.requests) == calls_before  # le backend n'est JAMAIS appelé
    # Retour au pass-through, toujours sans redéploiement.
    os.environ["LLM_GUARDRAILS"] = "noop"
    assert client.post("/llm/v1/chat/completions", json=_PAYLOAD, headers=relay_headers).status_code == 200


def test_custom_transform_guardrail_pluggable_by_registry():
    """Brancher une vraie règle = une classe enregistrée + un nom dans la config."""
    _, client, recorder = _setup()
    relay_headers = enroll(client)

    from app.llm.guardrails import GUARDRAIL_REGISTRY, Direction, Guardrail, GuardrailResult

    class RedactGuardrail(Guardrail):
        name = "redact_test"

        def check(self, payload, direction, ctx):
            if direction == Direction.REQUEST and isinstance(payload, dict):
                redacted = dict(payload)
                redacted["messages"] = [
                    {**m, "content": "[REDACTED]"} for m in payload.get("messages", [])
                ]
                return GuardrailResult(action="transform", payload=redacted)
            return GuardrailResult(action="allow")

    GUARDRAIL_REGISTRY[RedactGuardrail.name] = RedactGuardrail
    try:
        os.environ["LLM_GUARDRAILS"] = "redact_test"
        res = client.post("/llm/v1/chat/completions", json=_PAYLOAD, headers=relay_headers)
        assert res.status_code == 200
        outbound = json.loads(recorder.requests[-1].content)
        assert outbound["messages"][0]["content"] == "[REDACTED]"
    finally:
        GUARDRAIL_REGISTRY.pop(RedactGuardrail.name, None)


def test_response_direction_guardrail_denies_output():
    """Le hook SORTIE est réel : un deny sur la réponse bloque sans toucher au cœur."""
    _, client, _ = _setup()
    relay_headers = enroll(client)

    from app.llm.guardrails import GUARDRAIL_REGISTRY, Direction, Guardrail, GuardrailResult

    class DenyResponseGuardrail(Guardrail):
        name = "deny_response_test"

        def check(self, payload, direction, ctx):
            if direction == Direction.RESPONSE:
                return GuardrailResult(action="deny", reason="output blocked (test)")
            return GuardrailResult(action="allow")

    GUARDRAIL_REGISTRY[DenyResponseGuardrail.name] = DenyResponseGuardrail
    try:
        os.environ["LLM_GUARDRAILS"] = "deny_response_test"
        res = client.post("/llm/v1/chat/completions", json=_PAYLOAD, headers=relay_headers)
        assert res.status_code == 403
        assert "output blocked" in res.json()["error"]["message"]
    finally:
        GUARDRAIL_REGISTRY.pop(DenyResponseGuardrail.name, None)


# ── Critère 9 : bascule de backend pilotée par config, sans code ─────────────

def test_backend_registry_routes_by_model_map():
    _, client, recorder = _setup()
    relay_headers = enroll(client)
    os.environ["LLM_API_TOKEN_B"] = "backend-b-token"  # nosec B105: valeur factice
    os.environ["LLM_BACKENDS"] = json.dumps({
        "backends": {"b": {"base_url": "https://backend-b.test/v1", "token_env": "LLM_API_TOKEN_B"}},
        "model_map": {"b-*": "b"},
    })
    # Modèle mappé → backend B, avec SA clé.
    res = client.post("/llm/v1/chat/completions",
                      json={**_PAYLOAD, "model": "b-chat"}, headers=relay_headers)
    assert res.status_code == 200
    outbound = recorder.requests[-1]
    assert outbound.url.host == "backend-b.test"
    assert outbound.headers["authorization"] == "Bearer backend-b-token"
    # Modèle non mappé → backend par défaut (LLM_BASE_URL), sans redéploiement.
    res = client.post("/llm/v1/chat/completions",
                      json={**_PAYLOAD, "model": "test-model"}, headers=relay_headers)
    assert res.status_code == 200
    assert recorder.requests[-1].url.host == "backend.test"


# ── Streaming : le pipeline forwarde chunk par chunk (zéro bufferisation) ────

def test_wrap_stream_forwards_chunk_by_chunk():
    import asyncio

    from app.llm.auth import LlmIdentity
    from app.llm.pipeline import Interceptor, InterceptorPipeline, LlmRequestContext

    seen_before_end: list[bytes] = []

    class CountingInterceptor(Interceptor):
        name = "counting"

        def on_chunk(self, ctx, chunk):
            seen_before_end.append(chunk)
            return chunk

    upstream = [b"data: un\n\n", b"data: deux\n\n", b"data: [DONE]\n\n"]

    async def fake_upstream():
        for chunk in upstream:
            yield chunk

    async def consume():
        ctx = LlmRequestContext(identity=LlmIdentity("", "", "none"),
                                trace_id="t", route="chat/completions", stream=True)
        pipeline = InterceptorPipeline([CountingInterceptor()])
        out = []
        async for chunk in pipeline.wrap_stream(ctx, fake_upstream()):
            # Chaque chunk sort du pipeline dès qu'il entre : au moment où on
            # reçoit le chunk i, le pipeline n'a PAS encore vu le chunk i+1.
            assert len(seen_before_end) == len(out) + 1
            out.append(chunk)
        return out

    forwarded = asyncio.run(consume())
    assert forwarded == upstream  # intacts, dans l'ordre, un par un


# ── Critère 7 (unitaire) : compteurs cohérents entre « réplicas » ─────────────
# Deux instances de PostgresQuotaStore (≙ deux pods) partagent le même Postgres
# factice : le compteur est global, pas par instance.

class _FakeQuotaCursor:
    def __init__(self, shared: dict):
        self._shared = shared
        self._row = None

    def execute(self, sql, params=None):
        if "INSERT INTO llm_quota_counters" in sql:
            win = int(params["win"])
            window_index = int(time.time() // win)
            key = (params["subject"], window_index)
            self._shared[key] = self._shared.get(key, 0) + 1
            retry_after = max(1, int((window_index + 1) * win - time.time()) + 1)
            self._row = (self._shared[key], retry_after)
        # DELETE (purge) : no-op

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeQuotaConn:
    autocommit = False

    def __init__(self, shared):
        self._shared = shared

    def cursor(self):
        return _FakeQuotaCursor(self._shared)

    def close(self):
        pass


def test_quota_counters_shared_across_replicas(monkeypatch):
    import sys
    import types

    shared: dict = {}
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = lambda url: _FakeQuotaConn(shared)
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/bootstrap")

    from app.llm.throttle import PostgresQuotaStore

    replica_a, replica_b = PostgresQuotaStore(), PostgresQuotaStore()
    results = []
    for replica in (replica_a, replica_b, replica_a, replica_b):  # LB round-robin
        results.append(replica.incr("user-1", limit=3, window_seconds=60))

    counts = [count for _, count, _ in results]
    assert counts == [1, 2, 3, 4]  # compteur GLOBAL, pas par réplica
    allowed = [ok for ok, _, _ in results]
    assert allowed == [True, True, True, False]  # la 4ᵉ dépasse limit=3 → 429
    assert all(retry >= 1 for _, _, retry in results)
