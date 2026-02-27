from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    binaries_mode: str = Field(default="presign")  # "presign" or "proxy"
    presign_ttl_seconds: int = Field(default=300)
    s3_endpoint_url: str | None = Field(default=None)
    aws_region: str | None = Field(default=None)

    # PostgreSQL (strictement PSQL_*)
    psql_host: str | None = Field(default=None, validation_alias="PSQL_HOST")
    psql_port: int = Field(default=5432, validation_alias="PSQL_PORT")
    psql_database: str | None = Field(default=None, validation_alias="PSQL_DATABASE")
    psql_user: str | None = Field(default=None, validation_alias="PSQL_USER")
    psql_password: str | None = Field(default=None, validation_alias="PSQL_PASSWORD")
    psql_admin_user: str | None = Field(default=None, validation_alias="PSQL_ADMIN_USER")
    psql_admin_password: str | None = Field(default=None, validation_alias="PSQL_ADMIN_PASSWORD")


settings = Settings()
