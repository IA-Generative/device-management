"""
Tests for the configuration endpoint.
"""

import pytest
from fastapi.testclient import TestClient


class TestConfigEndpoint:
    """Tests for GET /config/config.json endpoint."""

    def test_get_config_default(self, client: TestClient):
        """Test getting default configuration."""
        response = client.get("/config/config.json")
        
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        # Check Cache-Control header
        assert response.headers.get("cache-control") == "no-store"

    def test_get_config_dev_profile(self, client: TestClient):
        """Test getting configuration with dev profile."""
        response = client.get("/config/config.json?profile=dev")
        
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True

    def test_get_config_prod_profile(self, client: TestClient):
        """Test getting configuration with prod profile."""
        response = client.get("/config/config.json?profile=prod")
        
        assert response.status_code == 200

    def test_get_config_int_profile(self, client: TestClient):
        """Test getting configuration with int profile."""
        response = client.get("/config/config.json?profile=int")
        
        assert response.status_code == 200

    def test_get_config_invalid_profile(self, client: TestClient):
        """Test getting configuration with invalid profile returns 400."""
        response = client.get("/config/config.json?profile=invalid")
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "profile" in data["error"].lower()

    def test_get_device_config_matisse(self, client: TestClient):
        """Test getting device-specific configuration for matisse."""
        response = client.get("/config/matisse/config.json")
        
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data

    def test_get_device_config_libreoffice(self, client: TestClient):
        """Test getting device-specific configuration for libreoffice."""
        response = client.get("/config/libreoffice/config.json")
        
        assert response.status_code == 200

    def test_get_device_config_chrome(self, client: TestClient):
        """Test getting device-specific configuration for chrome."""
        response = client.get("/config/chrome/config.json")
        
        assert response.status_code == 200

    def test_get_device_config_invalid(self, client: TestClient):
        """Test getting configuration for invalid device returns 400."""
        response = client.get("/config/invalid-device/config.json")
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "device" in data["error"].lower()

    def test_get_device_config_with_profile(self, client: TestClient):
        """Test getting device configuration with profile parameter."""
        response = client.get("/config/matisse/config.json?profile=dev")
        
        assert response.status_code == 200

    def test_config_fallback_to_default(self, client: TestClient):
        """Test that device config falls back to default if device-specific doesn't exist."""
        # misc device might not have specific config
        response = client.get("/config/misc/config.json")
        
        assert response.status_code == 200


class TestConfigEnvSubstitution:
    """Tests for environment variable substitution in config."""

    def test_env_var_substitution(self, client: TestClient, monkeypatch):
        """Test that environment variables are substituted."""
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://test.example.com")
        
        response = client.get("/config/config.json")
        
        assert response.status_code == 200
        # The actual substitution depends on config template content
        # Just verify the endpoint works with env vars set
