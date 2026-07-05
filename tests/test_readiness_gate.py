"""Stage 9: readiness gate — a pod that never loaded its config must not serve."""
import pytest
from fastapi.testclient import TestClient

import app.runtime_config as rc
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_gate():
    rc.disable_request_gate()
    rc._config_ready = False
    yield
    rc.disable_request_gate()
    rc._config_ready = False


def test_gate_inactive_by_default_serves_normally(client):
    # No sync started -> gate inactive -> liveness works.
    assert client.get("/livez").status_code == 200


def test_gate_active_blocks_business_paths_with_retry(client):
    rc.enable_request_gate()  # config never loaded
    r = client.get("/config/config.json")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "3"
    assert r.json().get("retry") is True


def test_gate_active_exempts_probes(client):
    rc.enable_request_gate()
    assert client.get("/livez").status_code == 200
    assert client.get("/healthz").status_code == 200
    # readyz reports not-ready while gated
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json().get("ready") is False


def test_readyz_ok_once_ready(client):
    rc.enable_request_gate()
    rc._config_ready = True
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json().get("ready") is True
