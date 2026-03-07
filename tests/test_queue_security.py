import importlib
import os
import sys

from fastapi.testclient import TestClient


def _load_module(*, queue_admin_token: str = "queue-admin-secret"):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_TOKEN_SIGNING_KEY"] = "unit-test-signing-key"
    os.environ["DM_TELEMETRY_REQUIRE_TOKEN"] = "true"
    os.environ["DM_QUEUE_ENABLED"] = "true"
    os.environ["DM_QUEUE_ADMIN_TOKEN"] = queue_admin_token
    os.environ["DM_RUNTIME_MODE"] = "api"

    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def test_queue_stats_requires_admin_token():
    mod = _load_module()
    client = TestClient(mod.app)

    denied = client.get("/ops/queue/stats")
    assert denied.status_code == 401

    allowed = client.get("/ops/queue/stats", headers={"X-Queue-Admin-Token": "queue-admin-secret"})
    assert allowed.status_code in (200, 503)


def test_queue_health_rejects_wrong_admin_token():
    mod = _load_module()
    client = TestClient(mod.app)

    denied = client.get("/ops/queue/health", headers={"X-Queue-Admin-Token": "wrong"})
    assert denied.status_code == 401


def test_queue_ops_return_503_when_admin_token_not_configured():
    mod = _load_module(queue_admin_token="")
    client = TestClient(mod.app)

    res = client.get("/ops/queue/health")
    assert res.status_code == 503


def test_telemetry_accepts_sql_like_payload_without_breaking_queue(monkeypatch):
    mod = _load_module()
    client = TestClient(mod.app)
    captured = {}

    class FakeQueue:
        def enqueue(self, *, topic, payload, dedupe_key=None, run_after_seconds=0, max_attempts=None):
            captured["topic"] = topic
            captured["payload"] = payload
            captured["dedupe_key"] = dedupe_key
            return "job-sql", "pending"

    monkeypatch.setattr(mod, "_get_queue_manager", lambda: FakeQueue())

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    token = token_res.json()["telemetryKey"]
    payload = b'{"query":"SELECT * FROM users; DROP TABLE queue_jobs;--"}'

    res = client.post(
        "/telemetry/v1/traces",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": "sql-injection-attempt-1",
        },
    )
    assert res.status_code == 202
    assert captured["topic"] == "telemetry.forward"
    assert captured["payload"]["body_b64"]
    assert str(captured["dedupe_key"]).startswith("telemetry:")
    assert str(captured["dedupe_key"]).endswith(":sql-injection-attempt-1")


def test_telemetry_uses_idempotency_header_for_dedupe(monkeypatch):
    mod = _load_module()
    client = TestClient(mod.app)
    dedupe_values: list[str | None] = []

    class FakeQueue:
        def enqueue(self, *, topic, payload, dedupe_key=None, run_after_seconds=0, max_attempts=None):
            dedupe_values.append(dedupe_key)
            return "job-dup", "pending"

    monkeypatch.setattr(mod, "_get_queue_manager", lambda: FakeQueue())

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    token = token_res.json()["telemetryKey"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": "same-event-id",
    }
    first = client.post("/telemetry/v1/traces", data=b'{"k":"v"}', headers=headers)
    second = client.post("/telemetry/v1/traces", data=b'{"k":"v"}', headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    assert len(dedupe_values) == 2
    assert dedupe_values[0] == dedupe_values[1]
    assert str(dedupe_values[0]).startswith("telemetry:")
    assert str(dedupe_values[0]).endswith(":same-event-id")


def test_telemetry_rejects_oversized_payload():
    mod = _load_module()
    client = TestClient(mod.app)

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    token = token_res.json()["telemetryKey"]
    oversized = b"x" * (3 * 1024 * 1024)

    res = client.post(
        "/telemetry/v1/traces",
        data=oversized,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert res.status_code == 413
