"""
Non-regression tests for operational endpoints:
- GET /livez
- GET /healthz
- GET /ops/metrics (Prometheus)
- GET /ops/health/full (JSON)
- GET /catalog/api/plugins (public catalog)
- GET /catalog/api/plugins/{slug}/icon.{ext}
- GET /config/{device}/config.json (enrichment + cache bypass)
- POST /config/cache/clear
- POST /api/plugins/{slug}/deploy (unified deploy)
- GET /catalog/{slug}/download (pull-on-miss)

All DB interactions are mocked — no real PostgreSQL required.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Module bootstrap
# ---------------------------------------------------------------------------

def _setup_env():
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_RELAY_ENABLED"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_RELAY_REQUIRE_KEY_FOR_SECRETS"] = "false"
    os.environ["DM_TELEMETRY_TOKEN_SIGNING_KEY"] = "test-signing-key-1234"
    os.environ["DM_TELEMETRY_REQUIRE_TOKEN"] = "false"
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"
    os.environ["DM_QUEUE_ADMIN_TOKEN"] = "test-admin-token"
    os.environ["PUBLIC_BASE_URL"] = "https://test.example.com"


def _make_fake_psycopg2():
    mod = types.ModuleType("psycopg2")
    mod.connect = MagicMock()
    mod.Error = Exception
    pool_mod = types.ModuleType("psycopg2.pool")
    pool_mod.ThreadedConnectionPool = MagicMock(side_effect=Exception("no pool in tests"))
    mod.pool = pool_mod
    return mod


def _load_module():
    _setup_env()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    fake = _make_fake_psycopg2()
    sys.modules["psycopg2"] = fake
    sys.modules["psycopg2.pool"] = fake.pool
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    mod.psycopg2 = fake
    return mod


def _make_cursor(rows_by_fragment: dict):
    """Build a cursor mock that responds based on SQL fragment matching."""
    cur = MagicMock()
    _last_sql = [""]

    def _execute(sql, params=None):
        _last_sql[0] = sql

    def _fetchall():
        for frag, rows in rows_by_fragment.items():
            if frag in _last_sql[0]:
                return list(rows)
        return []

    def _fetchone():
        rows = _fetchall()
        return rows[0] if rows else None

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.description = [("col",)]
    return cur


def _make_conn(cur):
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mod():
    return _load_module()


@pytest.fixture(scope="module")
def client(mod):
    return TestClient(mod.app)


# ---------------------------------------------------------------------------
# Tests: Liveness & Health
# ---------------------------------------------------------------------------

class TestLiveness:
    def test_livez_always_200(self, client):
        resp = client.get("/livez")
        assert resp.status_code == 200

    def test_healthz_returns_json(self, client):
        resp = client.get("/healthz")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "status" in data


# ---------------------------------------------------------------------------
# Tests: Prometheus Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_metrics_returns_text(self, client, mod):
        cur = _make_cursor({
            "provisioning": [(42,)],
            "device_connections": [(100,)],
            "queue_jobs": [(3,)],
            "queue_job_dead_letters": [(0,)],
            "campaigns": [(2,)],
        })
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/ops/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        assert "dm_service_up" in body
        assert "dm_devices_enrolled_total 42" in body
        assert "dm_devices_active_7d 100" in body
        assert "dm_queue_pending 3" in body
        assert "dm_queue_dead 0" in body
        assert "dm_campaigns_active 2" in body

    def test_metrics_without_db(self, client, mod):
        """Metrics should not crash if DB is unreachable."""
        with patch.object(mod.psycopg2, "connect", side_effect=Exception("conn refused")):
            resp = client.get("/ops/metrics")
        assert resp.status_code == 200
        assert "dm_service_up{service=\"postgres\"} 0" in resp.text

    def test_metrics_format_prometheus(self, client, mod):
        """Verify Prometheus text exposition format."""
        cur = _make_cursor({
            "provisioning": [(0,)],
            "device_connections": [(0,)],
            "queue_jobs": [(0,)],
            "queue_job_dead_letters": [(0,)],
            "campaigns": [(0,)],
        })
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/ops/metrics")
        for line in resp.text.strip().split("\n"):
            assert line.startswith("#") or " " in line, f"Invalid Prometheus line: {line}"


# ---------------------------------------------------------------------------
# Tests: Health Full
# ---------------------------------------------------------------------------

class TestHealthFull:
    def test_health_full_db_ok(self, client, mod):
        cur = _make_cursor({"SELECT 1": [(1,)]})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/ops/health/full")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "services" in data
        assert "checked_at" in data
        assert data["services"]["postgres"]["status"] == "ok"

    def test_health_full_db_down(self, client, mod):
        with patch.object(mod.psycopg2, "connect", side_effect=Exception("refused")):
            resp = client.get("/ops/health/full")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert data["services"]["postgres"]["status"] == "error"


# ---------------------------------------------------------------------------
# Tests: Config Cache
# ---------------------------------------------------------------------------

class TestConfigCache:
    def test_cache_clear_endpoint(self, client):
        resp = client.post("/config/cache/clear")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_cache_bypassed_with_enrichment_headers(self, client, mod):
        """Requests with X-Plugin-Version must NOT be served from cache."""
        cur = _make_cursor({
            "plugins": [("test-device", "libreoffice", 1, "slug")],
            "config_template": [('{"configVersion":1,"default":{"enabled":true}}',)],
        })
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            # First call: generic (cacheable)
            r1 = client.get("/config/config.json?profile=prod")
            # Second call: with enrichment header (must bypass cache)
            r2 = client.get("/config/config.json?profile=prod",
                            headers={"X-Plugin-Version": "1.0.0"})
        # Both should succeed
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Enriched request must have no-store
        assert r2.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Tests: Version Comparison
# ---------------------------------------------------------------------------

class TestVersionComparison:
    def test_parse_version_tuple_3_segments(self, mod):
        assert mod._parse_version_tuple("1.2.3") == (1, 2, 3)

    def test_parse_version_tuple_5_segments(self, mod):
        assert mod._parse_version_tuple("0.0.1.0.4") == (0, 0, 1, 0, 4)

    def test_parse_version_tuple_1_segment(self, mod):
        assert mod._parse_version_tuple("42") == (42,)

    def test_parse_version_tuple_invalid(self, mod):
        assert mod._parse_version_tuple("abc") == (0,)

    def test_version_comparison_5_segments(self, mod):
        """Regression: versions with 5 segments must compare correctly."""
        v1 = mod._parse_version_tuple("0.0.1.0.3")
        v2 = mod._parse_version_tuple("0.0.1.0.4")
        assert v1 < v2

    def test_version_comparison_mixed_lengths(self, mod):
        v1 = mod._parse_version_tuple("1.0")
        v2 = mod._parse_version_tuple("1.0.1")
        assert v1 < v2


# ---------------------------------------------------------------------------
# Tests: Public Catalog API
# ---------------------------------------------------------------------------

class TestCatalogAPI:
    def test_catalog_api_plugins_empty(self, client, mod):
        cur = _make_cursor({})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/catalog/api/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert "plugins" in data
        assert "total" in data
        assert data["total"] == 0

    def test_catalog_api_cors_headers(self, client, mod):
        cur = _make_cursor({})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/catalog/api/plugins")
        assert resp.headers.get("access-control-allow-origin") == "*"

    def test_catalog_api_plugin_not_found(self, client, mod):
        cur = _make_cursor({})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/catalog/api/plugins/nonexistent")
        assert resp.status_code == 404

    def test_catalog_icon_not_found(self, client, mod):
        cur = _make_cursor({})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/catalog/api/plugins/nonexistent/icon.png")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Deploy API Auth
# ---------------------------------------------------------------------------

class TestDeployAPI:
    def test_deploy_requires_token(self, client):
        resp = client.post("/api/plugins/test/deploy")
        assert resp.status_code == 401

    def test_deploy_rejects_bad_token(self, client):
        resp = client.post("/api/plugins/test/deploy",
                           headers={"X-Admin-Token": "wrong-token"})
        assert resp.status_code == 401

    def test_deploy_requires_binary(self, client):
        resp = client.post("/api/plugins/test/deploy",
                           headers={"X-Admin-Token": "test-admin-token"})
        # 400 or 422 (no binary provided)
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Tests: Download Route
# ---------------------------------------------------------------------------

class TestDownload:
    def test_download_not_found(self, client, mod):
        cur = _make_cursor({})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            resp = client.get("/catalog/nonexistent/download")
        assert resp.status_code == 404

    def test_download_filename_parsing(self, mod):
        """Regression: filename with multi-segment version must parse correctly."""
        # Simulate the parsing logic from catalog_download_file
        filename = "mirai-libreoffice-0.0.1.0.4.oxt"
        slug = "mirai-libreoffice"
        known_ext = (".oxt", ".xpi", ".crx", ".bin")
        base = filename
        for ext in known_ext:
            if base.endswith(ext):
                base = base[:-len(ext)]
                break
        version = base.removeprefix(f"{slug}-")
        assert version == "0.0.1.0.4"


# ---------------------------------------------------------------------------
# Tests: Provisioning Upsert
# ---------------------------------------------------------------------------

class TestProvisioning:
    def test_upsert_provisioning_sql_syntax(self, mod):
        """Regression: ON CONFLICT must match partial unique index."""
        cur = _make_cursor({"SELECT 1": [(1,)]})
        conn = _make_conn(cur)
        with patch.object(mod.psycopg2, "connect", return_value=conn):
            mod._upsert_provisioning(
                email="test@test.com",
                client_uuid="00000000-0000-0000-0000-000000000001",
                device_name="test",
                encryption_key="test",
            )
        # Verify the SQL uses the partial index predicate
        call_args = cur.execute.call_args_list
        sql = call_args[-1][0][0]
        assert "WHERE status IN" in sql
        assert "ON CONFLICT (client_uuid)" in sql


# ---------------------------------------------------------------------------
# Tests: Security Headers
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    def test_security_headers_on_livez(self, client):
        resp = client.get("/livez")
        assert resp.headers.get("x-frame-options") == "DENY"
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    def test_files_api_requires_token(self, client):
        resp = client.get("/api/files")
        assert resp.status_code == 403

    def test_files_api_rejects_bad_token(self, client):
        resp = client.get("/api/files", headers={"x-admin-token": "wrong"})
        assert resp.status_code == 403
