import os

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


def _default_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL", "")
    if os.getenv("RELOAD", "").lower() == "true":
        return "postgresql://dev:dev@localhost:5432/bootstrap"
    return ""


def _env_default(*keys: str, default: str) -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value
    return default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DM_", extra="ignore")

    # API
    allow_origins: str = Field(default="*")  # CSV list
    max_body_size_mb: int = Field(default=10)

    # Config endpoint
    config_enabled: bool = Field(default=True)
    app_env: str = Field(default="dev")
    enroll_url: str = Field(default="/enroll")

    # Local storage for enroll payloads
    enroll_dir: str = Field(default="/data/enroll")
    store_enroll_locally: bool = Field(default=True)

    # S3 for enroll payloads (optional)
    store_enroll_s3: bool = Field(default=False)
    s3_bucket: str | None = Field(default=None)
    s3_prefix_enroll: str = Field(default="enroll/")

    # S3 binaries
    s3_prefix_binaries: str = Field(default="binaries/")
    binaries_mode: str = Field(default="presign")  # "presign" or "proxy" or "local"
    presign_ttl_seconds: int = Field(default=300)
    s3_endpoint_url: str | None = Field(default=None)
    aws_region: str | None = Field(default=None)
    local_binaries_dir: str = Field(default="/data/content/binaries")
    config_dir: str = Field(default="/data/content/config")

    # Telemetry relay / token rotation
    telemetry_enabled: bool = Field(default=True)
    telemetry_public_endpoint: str = Field(
        default_factory=lambda: _env_default("TELEMETRY_PUBLIC_ENDPOINT", default="/telemetry/v1/traces")
    )
    telemetry_authorization_type: str = Field(
        default_factory=lambda: _env_default("TELEMETRY_AUTHORIZATION_TYPE", default="Bearer")
    )
    telemetry_upstream_endpoint: str = Field(
        default_factory=lambda: _env_default(
            "TELEMETRY_UPSTREAM_ENDPOINT",
            default="https://telemetry.minint.fr/v1/traces",
        )
    )
    telemetry_upstream_auth_type: str = Field(
        default_factory=lambda: _env_default("TELEMETRY_UPSTREAM_AUTH_TYPE", default="Bearer")
    )
    telemetry_upstream_key: str = Field(
        default_factory=lambda: _env_default("TELEMETRY_UPSTREAM_KEY", default="")
    )
    telemetry_token_ttl_seconds: int = Field(default=300)
    telemetry_token_signing_key: str = Field(default="")
    telemetry_require_token: bool = Field(default=True)
    telemetry_max_body_size_mb: int = Field(default=2)

    # Relay auth (plugin -> relay -> DM authorize)
    relay_enabled: bool = Field(default=True)
    relay_proxy_shared_token: str = Field(default="")
    relay_secret_pepper: str = Field(default="change-me-relay-pepper")
    relay_key_ttl_seconds: int = Field(default=30 * 24 * 3600)
    relay_allowed_targets_csv: str = Field(default="keycloak")
    relay_require_key_for_secrets: bool = Field(default=True)

    # Database (optional, used by local tooling)
    database_url: str = Field(default_factory=_default_database_url)


settings = Settings()
