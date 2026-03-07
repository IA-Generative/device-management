import base64
import importlib
import json
import os
import sys
import uuid

from fastapi.testclient import TestClient


def _mk_fake_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def _enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_enc(header)}.{_enc(payload)}.sig"


def _load_module(
    *,
    queue_enabled: bool = True,
    store_enroll_s3: bool = False,
    binaries_mode: str = "local",
    s3_bucket: str = "",
):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "true" if store_enroll_s3 else "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_TOKEN_SIGNING_KEY"] = "unit-test-signing-key"
    os.environ["DM_TELEMETRY_REQUIRE_TOKEN"] = "true"
    os.environ["DM_TELEMETRY_PUBLIC_ENDPOINT"] = "/telemetry/v1/traces"
    os.environ["DM_QUEUE_ENABLED"] = "true" if queue_enabled else "false"
    os.environ["DM_QUEUE_ADMIN_TOKEN"] = "queue-admin-secret"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DM_RUNTIME_MODE"] = "api"
    os.environ["DM_BINARIES_MODE"] = binaries_mode
    os.environ["DM_S3_BUCKET"] = s3_bucket

    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def test_queue_health_disabled():
    mod = _load_module(queue_enabled=False)
    client = TestClient(mod.app)
    res = client.get("/ops/queue/health", headers={"X-Queue-Admin-Token": "queue-admin-secret"})
    assert res.status_code == 200
    body = res.json()
    assert body["queue"]["enabled"] is False
    assert body["queue"]["status"] == "disabled"


def test_telemetry_is_queued_when_queue_enabled(monkeypatch):
    mod = _load_module(queue_enabled=True)
    client = TestClient(mod.app)

    class FakeQueue:
        def enqueue(self, *, topic, payload, dedupe_key=None, run_after_seconds=0, max_attempts=None):
            assert topic == "telemetry.forward"
            assert "body_b64" in payload
            return "job-test-1", "pending"

    monkeypatch.setattr(mod, "_get_queue_manager", lambda: FakeQueue())

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    assert token_res.status_code == 200
    token = token_res.json().get("telemetryKey")
    assert token

    res = client.post(
        "/telemetry/v1/traces",
        data=b'{"resourceSpans":[]}',
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body.get("ok") is True
    assert body.get("queued") is True
    assert body.get("jobId") == "job-test-1"


def test_enroll_is_queued_when_queue_enabled(monkeypatch):
    mod = _load_module(queue_enabled=True)
    client = TestClient(mod.app)
    token = _mk_fake_jwt({"email": "queue-enroll@example.com", "exp": 4102444800})

    class FakeQueue:
        def enqueue(self, *, topic, payload, dedupe_key=None, run_after_seconds=0, max_attempts=None):
            assert topic == "enroll.process"
            assert payload.get("body_b64")
            assert payload.get("email") == "queue-enroll@example.com"
            return "job-enroll-1", "pending"

    monkeypatch.setattr(mod, "_get_queue_manager", lambda: FakeQueue())

    payload = {
        "device_name": "libreoffice",
        "plugin_uuid": str(uuid.uuid4()),
    }
    res = client.post(
        "/enroll",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Idempotency-Key": "enroll-queue-1",
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body.get("ok") is True
    assert body.get("queued") is True
    assert body.get("jobId") == "job-enroll-1"


def test_healthz_skips_s3_when_not_required_even_if_bucket_is_set():
    mod = _load_module(
        queue_enabled=False,
        store_enroll_s3=False,
        binaries_mode="local",
        s3_bucket="configured-but-unused",
    )
    client = TestClient(mod.app)
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["checks"]["s3"]["status"] == "skipped"


def test_metrics_exposes_queue_stats(monkeypatch):
    mod = _load_module(queue_enabled=True)
    client = TestClient(mod.app)

    class FakeQueue:
        def stats(self):
            return {
                "pending": 7,
                "processing": 3,
                "done": 20,
                "dead": 1,
                "total": 31,
                "oldest_pending_age_seconds": 12,
                "stale_processing": 0,
            }

    monkeypatch.setattr(mod, "_get_queue_manager", lambda: FakeQueue())

    res = client.get("/metrics")
    assert res.status_code == 200
    assert "dm_queue_enabled 1" in res.text
    assert "dm_queue_available 1" in res.text
    assert "dm_queue_pending_jobs 7" in res.text
    assert "dm_queue_processing_jobs 3" in res.text
    assert "dm_queue_oldest_pending_age_seconds 12" in res.text
