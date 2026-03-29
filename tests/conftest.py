"""Shared test fixtures — ensure env isolation between test modules."""

import os
import sys
import pytest


# Env vars that test modules may set and that must not leak between modules
_VOLATILE_ENV_KEYS = [
    "DATABASE_URL", "DATABASE_ADMIN_URL",
    "DM_RELAY_ENABLED", "DM_RELAY_SECRET_PEPPER", "DM_RELAY_PROXY_SHARED_TOKEN",
    "DM_CONFIG_DIR", "DM_CONFIG_PROFILE", "DM_AUTH_VERIFY_ACCESS_TOKEN",
    "DM_RELAY_REQUIRE_KEY_FOR_SECRETS", "DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS",
    "DM_TELEMETRY_TOKEN_SIGNING_KEY", "DM_TELEMETRY_REQUIRE_TOKEN",
    "DM_TELEMETRY_PUBLIC_ENDPOINT", "DM_TELEMETRY_ENABLED",
    "KEYCLOAK_ISSUER_URL", "PUBLIC_BASE_URL", "LLM_API_TOKEN",
]


@pytest.fixture(autouse=True)
def _isolate_env_and_modules():
    """Save env + sys.modules before each test, restore after."""
    saved_env = {k: os.environ.get(k) for k in _VOLATILE_ENV_KEYS}
    saved_psycopg2 = sys.modules.get("psycopg2")
    yield
    # Restore env
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Restore psycopg2 module
    if saved_psycopg2 is None:
        sys.modules.pop("psycopg2", None)
    else:
        sys.modules["psycopg2"] = saved_psycopg2
