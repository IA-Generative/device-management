"""
Pydantic schemas for all admin UI inputs.
Security rationale: all external data goes through Pydantic validation
before touching the database or business logic.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator
import re


class CohortCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    type: str = Field(..., pattern=r"^(manual|percentage|email_pattern|keycloak_group)$")
    config: dict = Field(default_factory=dict)
    members: str = Field(default="")  # newline-separated for manual type

    @field_validator("name")
    @classmethod
    def name_slug_safe(cls, v: str) -> str:
        if not re.match(r"^[\w\-. ]+$", v):
            raise ValueError("Name must contain only letters, digits, hyphens, dots, spaces")
        return v.strip()


class CohortUpdate(BaseModel):
    description: Optional[str] = None
    config: Optional[dict] = None
    members: Optional[str] = None


class FlagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[\w_]+$")
    description: str = Field(default="")
    default_value: bool = Field(default=True)


class FlagDefaultUpdate(BaseModel):
    value: str = Field(..., pattern=r"^(true|false)$")


class FlagOverrideCreate(BaseModel):
    cohort_id: int
    value: bool
    min_plugin_version: Optional[str] = Field(default=None, max_length=50)


class ArtifactUpload(BaseModel):
    device_type: str = Field(..., pattern=r"^(libreoffice|matisse)$")
    platform_variant: str = Field(default="")
    version: str = Field(..., min_length=1, max_length=50, pattern=r"^\d+\.\d+\.\d+")
    changelog_url: str = Field(default="")


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="")
    type: str = Field(default="plugin_update", pattern=r"^(plugin_update|config_patch|feature_set)$")
    artifact_id: Optional[int] = None
    rollback_artifact_id: Optional[int] = None
    target_cohort_id: Optional[int] = None
    new_cohort_type: Optional[str] = None
    new_cohort_value: Optional[str] = None
    urgency: str = Field(default="normal", pattern=r"^(low|normal|critical)$")
    deadline_at: Optional[str] = None
    start_status: str = Field(default="draft", pattern=r"^(draft|active)$")


class CampaignAction(BaseModel):
    reason: str = Field(default="")
    comment: str = Field(default="")


class CampaignExpand(BaseModel):
    percentage: int = Field(..., ge=1, le=100)


class AuditFilter(BaseModel):
    actor: Optional[str] = None
    action: Optional[str] = None
    resource_type: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
