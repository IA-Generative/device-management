"""
Pydantic models for request/response validation.

This module defines strongly-typed models for all API endpoints,
ensuring proper validation and documentation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# --- Enums ---


class ProvisioningStatus(str, Enum):
    """Provisioning lifecycle status."""

    PENDING = "PENDING"
    ENROLLED = "ENROLLED"
    REVOKED = "REVOKED"
    FAILED = "FAILED"


class DeviceAction(str, Enum):
    """Device connection action types."""

    ENROLL = "ENROLL"
    CONFIG_GET = "CONFIG_GET"
    BINARY_GET = "BINARY_GET"
    HEALTHZ = "HEALTHZ"
    UNKNOWN = "UNKNOWN"


class DeviceName(str, Enum):
    """Allowed device names."""

    MATISSE = "matisse"
    LIBREOFFICE = "libreoffice"
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"
    MISC = "misc"


class ConfigProfile(str, Enum):
    """Configuration profiles."""

    DEV = "dev"
    PROD = "prod"
    INT = "int"
    LLAMA = "llama"
    GPTOSS = "gptoss"


# --- Request Models ---


class EnrollRequest(BaseModel):
    """Request payload for device enrollment."""

    model_config = ConfigDict(str_strip_whitespace=True)

    device_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Device/plugin identifier (e.g., matisse, libreoffice)",
        examples=["matisse"],
    )
    plugin_uuid: UUID = Field(
        ...,
        description="Unique plugin/client UUID",
        examples=["b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a"],
    )
    email: EmailStr = Field(
        ...,
        description="User email address",
        examples=["user@example.com"],
    )
    encryption_key_fingerprint: str | None = Field(
        default=None,
        max_length=500,
        description="Optional encryption key fingerprint for audit",
    )

    @field_validator("device_name")
    @classmethod
    def validate_device_name(cls, v: str) -> str:
        """Ensure device_name is not empty after stripping."""
        if not v or not v.strip():
            raise ValueError("device_name cannot be empty")
        return v.strip().lower()


# --- Response Models ---


class EnrollResponse(BaseModel):
    """Response for successful enrollment."""

    ok: Literal[True] = True
    stored: dict[str, str] = Field(
        default_factory=dict,
        description="Storage locations (local path, S3 URI)",
        examples=[{"local": "/data/enroll/123.json", "s3": "s3://bucket/enroll/123.json"}],
    )


class ErrorResponse(BaseModel):
    """Standard error response."""

    ok: Literal[False] = False
    error: str = Field(..., description="Error message")


class CheckStatus(BaseModel):
    """Individual health check status."""

    status: Literal["ok", "error", "skipped"]
    detail: str | None = None


class HealthzResponse(BaseModel):
    """Health check response (RFC 7807 Problem Details)."""

    type: str = Field(
        default="https://example.com/problems/dependency-check",
        description="Problem type URI",
    )
    title: str = Field(..., description="Short summary of the problem")
    status: int = Field(..., description="HTTP status code")
    detail: str = Field(..., description="Human-readable explanation")
    checks: dict[str, CheckStatus] = Field(
        default_factory=dict,
        description="Individual dependency check results",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="List of error messages (empty if healthy)",
    )


# --- Database Models (for documentation/type hints) ---


class ProvisioningRecord(BaseModel):
    """Provisioning database record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    email: str
    device_name: str
    client_uuid: UUID
    status: ProvisioningStatus
    encryption_key: str
    comments: str | None = None


class DeviceConnectionRecord(BaseModel):
    """Device connection audit log record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    email: str
    client_uuid: UUID
    action: DeviceAction
    encryption_key_fingerprint: str
    connected_at: datetime
    disconnected_at: datetime | None = None
    source_ip: str | None = None
    user_agent: str | None = None


# --- Internal DTOs ---


class EnrollmentData(BaseModel):
    """Internal DTO for enrollment processing."""

    device_name: str
    plugin_uuid: str
    email: str
    encryption_key: str = "unknown"
    source_ip: str | None = None
    user_agent: str | None = None


class StorageResult(BaseModel):
    """Result of storage operations."""

    local_path: str | None = None
    s3_uri: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Convert to response-friendly dict."""
        result: dict[str, str] = {}
        if self.local_path:
            result["local"] = self.local_path
        if self.s3_uri:
            result["s3"] = self.s3_uri
        return result
