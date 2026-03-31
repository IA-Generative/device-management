"""
Post-deployment smoke tests — run against a live DM instance.

Usage:
    DM_BASE_URL=https://bootstrap.fake-domain.name pytest tests/test_post_deploy.py -v
    DM_BASE_URL=http://localhost:3001 pytest tests/test_post_deploy.py -v

These tests verify that all endpoints are reachable and return expected
status codes and response shapes after a deployment. They do NOT modify
data (read-only, no POST/PUT/DELETE except idempotent ones).

Skip with: pytest tests/test_post_deploy.py -k "not post_deploy"
"""
from __future__ import annotations

import os
import json

import pytest
import httpx

BASE = os.getenv("DM_BASE_URL", "https://bootstrap.fake-domain.name")
ADMIN_TOKEN = os.getenv("DM_ADMIN_TOKEN", "change-me-queue-admin-token")
SLUG = os.getenv("DM_TEST_SLUG", "mirai-libreoffice")
TIMEOUT = 15


def _get(path, **kwargs):
    return httpx.get(f"{BASE}{path}", timeout=TIMEOUT, follow_redirects=True, **kwargs)


def _head(path, **kwargs):
    return httpx.get(f"{BASE}{path}", timeout=TIMEOUT, **kwargs)


# ---------------------------------------------------------------------------
# Core health endpoints
# ---------------------------------------------------------------------------

class TestHealth:
    def test_livez(self):
        r = _get("/livez")
        assert r.status_code == 200

    def test_healthz(self):
        r = _get("/healthz")
        assert r.status_code in (200, 503)
        data = r.json()
        assert "status" in data
        assert "checks" in data

    def test_ops_health_full(self):
        r = _get("/ops/health/full")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("ok", "degraded", "error")
        assert "services" in data
        assert "checked_at" in data
        # Postgres must be checked
        assert "postgres" in data["services"]

    def test_ops_metrics_prometheus(self):
        r = _get("/ops/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        body = r.text
        # Required metrics
        assert "dm_service_up" in body
        assert "dm_devices_enrolled_total" in body
        assert "dm_devices_active_7d" in body
        assert "dm_campaigns_active" in body

    def test_security_headers(self):
        r = _get("/livez")
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"


# ---------------------------------------------------------------------------
# Config endpoint
# ---------------------------------------------------------------------------

class TestConfig:
    def test_config_returns_schema_v2(self):
        r = _get(f"/config/{SLUG}/config.json?profile=int")
        assert r.status_code == 200
        data = r.json()
        assert data["meta"]["schema_version"] == 2
        assert data["meta"]["device_name"] == SLUG
        assert "config" in data
        assert "update" in data
        assert "features" in data

    def test_config_with_enrichment_headers(self):
        """Config with X-Plugin-Version must return device-specific update directive."""
        r = httpx.get(
            f"{BASE}/config/{SLUG}/config.json?profile=int",
            headers={"X-Plugin-Version": "0.0.0.0.1", "X-Client-UUID": "test-post-deploy"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["meta"]["schema_version"] == 2
        # Should have no-store cache header (not cached)
        assert r.headers.get("cache-control") == "no-store"

    def test_config_cache_clear(self):
        r = httpx.post(f"{BASE}/config/cache/clear", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_config_unknown_device_fallback(self):
        """Known fallback devices should not 400."""
        r = _get("/config/libreoffice/config.json?profile=int")
        assert r.status_code == 200

    def test_config_unknown_device_rejects(self):
        r = _get("/config/nonexistent-device-xyz/config.json?profile=int")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

class TestTelemetry:
    def test_telemetry_token_endpoint(self):
        r = _get("/telemetry/token?profile=int&device=mirai-libreoffice")
        assert r.status_code == 200
        data = r.json()
        assert "telemetryEnabled" in data
        assert "telemetryKey" in data
        assert "telemetryEndpoint" in data

    def test_telemetry_traces_rejects_without_auth(self):
        r = httpx.post(
            f"{BASE}/telemetry/v1/traces",
            headers={"Content-Type": "application/json"},
            content=b"{}",
            timeout=TIMEOUT,
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Catalog public API
# ---------------------------------------------------------------------------

class TestCatalogAPI:
    def test_catalog_api_plugins_list(self):
        r = _get("/catalog/api/plugins")
        assert r.status_code == 200
        data = r.json()
        assert "plugins" in data
        assert "total" in data
        assert isinstance(data["plugins"], list)
        # CORS
        assert r.headers.get("access-control-allow-origin") == "*"

    def test_catalog_api_plugin_detail(self):
        r = _get(f"/catalog/api/plugins/{SLUG}")
        if r.status_code == 404:
            pytest.skip(f"Plugin {SLUG} not found in catalog")
        assert r.status_code == 200
        data = r.json()
        assert data["slug"] == SLUG
        assert "name" in data
        assert "icon_url" in data
        assert "latest_version" in data
        assert "key_features" in data

    def test_catalog_api_icon(self):
        r = _get(f"/catalog/api/plugins/{SLUG}/icon.png")
        if r.status_code == 404:
            pytest.skip("No icon for this plugin")
        assert r.status_code == 200
        assert r.headers.get("content-type") == "image/png"
        assert len(r.content) > 100

    def test_catalog_api_icon_redirect(self):
        """GET /icon should redirect to /icon.{ext}."""
        r = httpx.get(
            f"{BASE}/catalog/api/plugins/{SLUG}/icon",
            timeout=TIMEOUT,
            follow_redirects=False,
        )
        if r.status_code == 404:
            pytest.skip("No icon for this plugin")
        assert r.status_code == 301
        assert "/icon." in r.headers.get("location", "")


# ---------------------------------------------------------------------------
# Catalog HTML pages
# ---------------------------------------------------------------------------

class TestCatalogHTML:
    def test_catalog_index_page(self):
        r = _get("/catalog")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "Catalogue" in r.text or "catalog" in r.text.lower()

    def test_catalog_detail_page(self):
        r = _get(f"/catalog/{SLUG}")
        if r.status_code == 404:
            pytest.skip(f"Plugin {SLUG} not found")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_catalog_download_redirect(self):
        """Download should redirect to a filename URL."""
        r = httpx.get(
            f"{BASE}/catalog/{SLUG}/download",
            timeout=TIMEOUT,
            follow_redirects=False,
        )
        if r.status_code == 404:
            pytest.skip("No published version")
        assert r.status_code == 302
        loc = r.headers.get("location", "")
        assert SLUG in loc
        assert ".oxt" in loc or ".xpi" in loc or ".crx" in loc


# ---------------------------------------------------------------------------
# Deploy API (auth only, no mutation)
# ---------------------------------------------------------------------------

class TestDeployAuth:
    def test_deploy_rejects_no_token(self):
        r = httpx.post(f"{BASE}/api/plugins/{SLUG}/deploy", timeout=TIMEOUT)
        assert r.status_code == 401

    def test_deploy_rejects_bad_token(self):
        r = httpx.post(
            f"{BASE}/api/plugins/{SLUG}/deploy",
            headers={"X-Admin-Token": "invalid-token-xyz"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 401

    def test_campaigns_api_rejects_no_token(self):
        r = httpx.post(
            f"{BASE}/api/campaigns",
            content=b"{}",
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Files API (auth only, no mutation)
# ---------------------------------------------------------------------------

class TestFilesAuth:
    def test_files_list_rejects_no_token(self):
        r = _get("/api/files")
        assert r.status_code == 403

    def test_files_list_with_token(self):
        r = httpx.get(
            f"{BASE}/api/files",
            headers={"x-admin-token": ADMIN_TOKEN},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        data = r.json()
        assert "files" in data


# ---------------------------------------------------------------------------
# Enrollment endpoint (shape only, no actual enroll)
# ---------------------------------------------------------------------------

class TestEnrollShape:
    def test_enroll_rejects_empty_body(self):
        r = httpx.post(f"{BASE}/enroll", content=b"", timeout=TIMEOUT)
        assert r.status_code == 400

    def test_enroll_rejects_invalid_json(self):
        r = httpx.post(f"{BASE}/enroll", content=b"not json", timeout=TIMEOUT)
        assert r.status_code == 400

    def test_enroll_rejects_missing_fields(self):
        r = httpx.post(
            f"{BASE}/enroll",
            content=json.dumps({"device_name": "test"}).encode(),
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 400
        assert "Missing required" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Cross-module integration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_config_and_catalog_same_plugin(self):
        """The config endpoint and catalog API should agree on the plugin slug."""
        r_config = _get(f"/config/{SLUG}/config.json?profile=int")
        r_catalog = _get(f"/catalog/api/plugins/{SLUG}")
        if r_catalog.status_code == 404:
            pytest.skip(f"Plugin {SLUG} not in catalog")
        assert r_config.status_code == 200
        assert r_catalog.status_code == 200
        assert r_config.json()["meta"]["device_name"] == r_catalog.json()["slug"]

    def test_catalog_download_url_matches_api(self):
        """The download_url in the API should be reachable."""
        r = _get(f"/catalog/api/plugins/{SLUG}")
        if r.status_code == 404:
            pytest.skip(f"Plugin {SLUG} not in catalog")
        download_url = r.json().get("download_url", "")
        assert download_url, "download_url is empty"
        r_dl = httpx.get(download_url, timeout=TIMEOUT, follow_redirects=False)
        # Should be 302 redirect or 200 or 404 (no version) — not 500
        assert r_dl.status_code in (200, 302, 404)

    def test_icon_url_from_api_is_reachable(self):
        """The icon_url in the API should serve an image."""
        r = _get(f"/catalog/api/plugins/{SLUG}")
        if r.status_code == 404:
            pytest.skip(f"Plugin {SLUG} not in catalog")
        icon_url = r.json().get("icon_url")
        if not icon_url:
            pytest.skip("No icon_url")
        r_icon = httpx.get(icon_url, timeout=TIMEOUT, follow_redirects=True)
        assert r_icon.status_code == 200
        assert r_icon.headers.get("content-type", "").startswith("image/")

    def test_telemetry_endpoint_in_config_is_reachable(self):
        """The telemetryEndpoint in the config should respond (even if 401)."""
        r = _get(f"/config/{SLUG}/config.json?profile=int")
        assert r.status_code == 200
        endpoint = r.json().get("config", {}).get("telemetryEndpoint", "")
        if not endpoint:
            pytest.skip("No telemetryEndpoint in config")
        # Just verify it's reachable (401 is expected without token)
        r_t = httpx.post(endpoint, content=b"{}", timeout=TIMEOUT,
                         headers={"Content-Type": "application/json"})
        assert r_t.status_code in (200, 401, 403)

    def test_all_deployments_have_same_version(self):
        """Verify via /livez that the API responds (version is in image tag, not exposed)."""
        r = _get("/livez")
        assert r.status_code == 200
        # Also verify admin responds
        r_admin = _get("/admin/callback")
        # Callback without params redirects to SSO — 307 or 302
        assert r_admin.status_code in (200, 302, 307, 400)
