"""
Tests for Pydantic models.
"""

import pytest
from pydantic import ValidationError
from uuid import UUID


class TestEnrollRequest:
    """Tests for EnrollRequest model."""

    def test_valid_request(self):
        """Test valid enrollment request."""
        from app.models import EnrollRequest
        
        request = EnrollRequest(
            device_name="matisse",
            plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            email="user@example.com",
        )
        
        assert request.device_name == "matisse"
        assert request.email == "user@example.com"
        assert isinstance(request.plugin_uuid, UUID)

    def test_device_name_stripped_and_lowercased(self):
        """Test device_name is normalized."""
        from app.models import EnrollRequest
        
        request = EnrollRequest(
            device_name="  MATISSE  ",
            plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            email="user@example.com",
        )
        
        assert request.device_name == "matisse"

    def test_empty_device_name_fails(self):
        """Test empty device_name raises ValidationError."""
        from app.models import EnrollRequest
        
        with pytest.raises(ValidationError) as exc_info:
            EnrollRequest(
                device_name="",
                plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
                email="user@example.com",
            )
        
        errors = exc_info.value.errors()
        assert any("device_name" in str(e) for e in errors)

    def test_whitespace_only_device_name_fails(self):
        """Test whitespace-only device_name raises ValidationError."""
        from app.models import EnrollRequest
        
        with pytest.raises(ValidationError):
            EnrollRequest(
                device_name="   ",
                plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
                email="user@example.com",
            )

    def test_invalid_uuid_fails(self):
        """Test invalid UUID raises ValidationError."""
        from app.models import EnrollRequest
        
        with pytest.raises(ValidationError):
            EnrollRequest(
                device_name="matisse",
                plugin_uuid="not-a-uuid",
                email="user@example.com",
            )

    def test_invalid_email_fails(self):
        """Test invalid email raises ValidationError."""
        from app.models import EnrollRequest
        
        with pytest.raises(ValidationError):
            EnrollRequest(
                device_name="matisse",
                plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
                email="not-an-email",
            )

    def test_optional_fingerprint(self):
        """Test encryption_key_fingerprint is optional."""
        from app.models import EnrollRequest
        
        # Without fingerprint
        request1 = EnrollRequest(
            device_name="matisse",
            plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            email="user@example.com",
        )
        assert request1.encryption_key_fingerprint is None
        
        # With fingerprint
        request2 = EnrollRequest(
            device_name="matisse",
            plugin_uuid="b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
            email="user@example.com",
            encryption_key_fingerprint="sha256:abc123",
        )
        assert request2.encryption_key_fingerprint == "sha256:abc123"


class TestEnrollResponse:
    """Tests for EnrollResponse model."""

    def test_default_values(self):
        """Test default response values."""
        from app.models import EnrollResponse
        
        response = EnrollResponse()
        
        assert response.ok is True
        assert response.stored == {}

    def test_with_storage_info(self):
        """Test response with storage information."""
        from app.models import EnrollResponse
        
        response = EnrollResponse(
            stored={
                "local": "/data/enroll/123.json",
                "s3": "s3://bucket/enroll/123.json",
            }
        )
        
        assert response.ok is True
        assert "local" in response.stored
        assert "s3" in response.stored


class TestErrorResponse:
    """Tests for ErrorResponse model."""

    def test_error_response(self):
        """Test error response creation."""
        from app.models import ErrorResponse
        
        response = ErrorResponse(error="Something went wrong")
        
        assert response.ok is False
        assert response.error == "Something went wrong"


class TestStorageResult:
    """Tests for StorageResult model."""

    def test_to_dict_empty(self):
        """Test to_dict with no storage."""
        from app.models import StorageResult
        
        result = StorageResult()
        
        assert result.to_dict() == {}

    def test_to_dict_with_local(self):
        """Test to_dict with local storage only."""
        from app.models import StorageResult
        
        result = StorageResult(local_path="/data/test.json")
        
        assert result.to_dict() == {"local": "/data/test.json"}

    def test_to_dict_with_both(self):
        """Test to_dict with both storage types."""
        from app.models import StorageResult
        
        result = StorageResult(
            local_path="/data/test.json",
            s3_uri="s3://bucket/test.json",
        )
        
        d = result.to_dict()
        assert d["local"] == "/data/test.json"
        assert d["s3"] == "s3://bucket/test.json"


class TestEnums:
    """Tests for enum models."""

    def test_provisioning_status_values(self):
        """Test ProvisioningStatus enum values."""
        from app.models import ProvisioningStatus
        
        assert ProvisioningStatus.PENDING.value == "PENDING"
        assert ProvisioningStatus.ENROLLED.value == "ENROLLED"
        assert ProvisioningStatus.REVOKED.value == "REVOKED"
        assert ProvisioningStatus.FAILED.value == "FAILED"

    def test_device_action_values(self):
        """Test DeviceAction enum values."""
        from app.models import DeviceAction
        
        assert DeviceAction.ENROLL.value == "ENROLL"
        assert DeviceAction.CONFIG_GET.value == "CONFIG_GET"
        assert DeviceAction.BINARY_GET.value == "BINARY_GET"

    def test_device_name_values(self):
        """Test DeviceName enum values."""
        from app.models import DeviceName
        
        expected = {"matisse", "libreoffice", "chrome", "edge", "firefox", "misc"}
        actual = {d.value for d in DeviceName}
        assert actual == expected

    def test_config_profile_values(self):
        """Test ConfigProfile enum values."""
        from app.models import ConfigProfile
        
        expected = {"dev", "prod", "int", "llama", "gptoss"}
        actual = {p.value for p in ConfigProfile}
        assert actual == expected
