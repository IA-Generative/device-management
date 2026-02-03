"""
Tests for the enrollment endpoint.
"""

import pytest
from fastapi.testclient import TestClient


class TestEnrollEndpoint:
    """Tests for POST /enroll endpoint."""

    def test_enroll_valid_payload(self, client: TestClient, valid_enroll_payload: dict):
        """Test successful enrollment with valid payload."""
        response = client.post("/enroll", json=valid_enroll_payload)
        
        assert response.status_code == 201
        data = response.json()
        assert data["ok"] is True
        assert "stored" in data

    def test_enroll_empty_body(self, client: TestClient):
        """Test enrollment with empty body returns 400."""
        response = client.post("/enroll", content=b"")
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "Empty body" in data["error"]

    def test_enroll_invalid_json(self, client: TestClient):
        """Test enrollment with invalid JSON returns 400."""
        response = client.post(
            "/enroll",
            content=b"not valid json",
            headers={"Content-Type": "application/json"},
        )
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "not valid JSON" in data["error"]

    def test_enroll_missing_device_name(self, client: TestClient):
        """Test enrollment without device_name returns 400."""
        payload = {
            "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            "email": "user@example.com",
        }
        response = client.post("/enroll", json=payload)
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "device_name" in data["error"].lower()

    def test_enroll_empty_device_name(self, client: TestClient):
        """Test enrollment with empty device_name returns 400."""
        payload = {
            "device_name": "",
            "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            "email": "user@example.com",
        }
        response = client.post("/enroll", json=payload)
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False

    def test_enroll_invalid_uuid(self, client: TestClient):
        """Test enrollment with invalid UUID returns 400."""
        payload = {
            "device_name": "matisse",
            "plugin_uuid": "not-a-uuid",
            "email": "user@example.com",
        }
        response = client.post("/enroll", json=payload)
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False

    def test_enroll_invalid_email(self, client: TestClient):
        """Test enrollment with invalid email returns 400."""
        payload = {
            "device_name": "matisse",
            "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            "email": "not-an-email",
        }
        response = client.post("/enroll", json=payload)
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False

    def test_enroll_options_returns_204(self, client: TestClient):
        """Test OPTIONS request returns 204 for CORS preflight."""
        response = client.options("/enroll")
        
        assert response.status_code == 204

    def test_enroll_put_method(self, client: TestClient, valid_enroll_payload: dict):
        """Test PUT method is also accepted."""
        response = client.put("/enroll", json=valid_enroll_payload)
        
        assert response.status_code == 201
        data = response.json()
        assert data["ok"] is True

    def test_enroll_device_name_normalized(self, client: TestClient):
        """Test device_name is normalized (lowercase, stripped)."""
        payload = {
            "device_name": "  MATISSE  ",
            "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            "email": "user@example.com",
        }
        response = client.post("/enroll", json=payload)
        
        assert response.status_code == 201

    def test_enroll_non_object_body(self, client: TestClient):
        """Test enrollment with non-object JSON body returns 400."""
        response = client.post("/enroll", json=["not", "an", "object"])
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "object" in data["error"].lower()
