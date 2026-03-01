import os

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


def _default_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL", "")
    if os.getenv("RELOAD", "").lower() == "true":
        return "postgresql://dev:dev@localhost:5432/bootstrap"
    return ""


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

    # Database (optional, used by local tooling)
    database_url: str = Field(default_factory=_default_database_url)


settings = Settings()
