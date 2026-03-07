import importlib
import os
import sys
import time

from fastapi.testclient import TestClient


def _load_module():
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
    os.environ["DM_RUNTIME_MODE"] = "api"

    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def test_queue_load_smoke(monkeypatch):
    mod = _load_module()
    client = TestClient(mod.app)

    class FakeQueue:
        def __init__(self) -> None:
            self.count = 0

        def enqueue(self, *, topic, payload, dedupe_key=None, run_after_seconds=0, max_attempts=None):
            self.count += 1
            return "job-load", "pending"

    queue = FakeQueue()
    monkeypatch.setattr(mod, "_get_queue_manager", lambda: queue)

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    assert token_res.status_code == 200
    token = token_res.json()["telemetryKey"]

    num_requests = 200
    latencies = []
    success = 0
    started_at = time.perf_counter()
    for _ in range(num_requests):
        start = time.perf_counter()
        res = client.post(
            "/telemetry/v1/traces",
            data=b"load",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-protobuf",
            },
        )
        latencies.append(time.perf_counter() - start)
        assert res.status_code == 202
        success += 1

    total_duration = max(0.001, time.perf_counter() - started_at)
    throughput_rps = success / total_duration
    failure_rate_pct = ((num_requests - success) / num_requests) * 100.0
    backlog = max(0, num_requests - queue.count)

    avg_latency_ms = (sum(latencies) / len(latencies)) * 1000.0
    print(
        f"queue_load_smoke metrics: avg_latency_ms={avg_latency_ms:.2f} "
        f"throughput_rps={throughput_rps:.2f} failure_rate_pct={failure_rate_pct:.2f} backlog={backlog}"
    )
    # Smoke objective: enqueued telemetry requests should remain very fast in local unit tests.
    assert avg_latency_ms < 50.0
    assert throughput_rps > 500.0
    assert failure_rate_pct == 0.0
    assert backlog == 0
