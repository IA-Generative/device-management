"""
Tests for the health check endpoint.
"""

import pytest
from fastapi.testclient import TestClient


class TestHealthzEndpoint:
    """Tests for GET /healthz endpoint."""

    def test_healthz_returns_200(self, client: TestClient):
        """Test healthz always returns 200 (even with errors)."""
        response = client.get("/healthz")
        
        assert response.status_code == 200
        assert response.headers.get("content-type") == "application/problem+json"

    def test_healthz_structure(self, client: TestClient):
        """Test healthz response follows RFC 7807 structure."""
        response = client.get("/healthz")
        
        data = response.json()
        
        # RFC 7807 required fields
        assert "type" in data
        assert "title" in data
        assert "status" in data
        assert "detail" in data
        
        # Custom fields
        assert "checks" in data
        assert "errors" in data
        
        # Verify structure of checks
        checks = data["checks"]
        assert isinstance(checks, dict)
        
        # Should have these check categories
        expected_checks = {"local_storage", "s3", "db"}
        assert expected_checks.issubset(set(checks.keys()))

    def test_healthz_check_status_format(self, client: TestClient):
        """Test each check has proper status format."""
        response = client.get("/healthz")
        data = response.json()
        
        for check_name, check_data in data["checks"].items():
            assert "status" in check_data
            assert check_data["status"] in ("ok", "error", "skipped")

    def test_healthz_errors_is_list(self, client: TestClient):
        """Test errors field is always a list."""
        response = client.get("/healthz")
        data = response.json()
        
        assert isinstance(data["errors"], list)

    def test_healthz_local_storage_skipped_when_disabled(self, client: TestClient, monkeypatch):
        """Test local storage check is skipped when disabled."""
        monkeypatch.setenv("DM_STORE_ENROLL_LOCALLY", "false")
        
        # Force reload to pick up new env
        import importlib
        from app import main
        importlib.reload(main)
        
        with TestClient(main.app) as c:
            response = c.get("/healthz")
        
        data = response.json()
        assert data["checks"]["local_storage"]["status"] == "skipped"
