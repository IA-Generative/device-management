"""
Pytest configuration and shared fixtures.

This module provides reusable fixtures for testing the Device Management API.
"""

import os
import sys
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Ensure the project root is in the path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Set up test environment variables."""
    # Disable storage by default for unit tests
    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    os.environ.setdefault("DM_CONFIG_ENABLED", "true")
    os.environ.setdefault("DM_CONFIG_PROFILE", "dev")
    os.environ.setdefault("DATABASE_URL", "")  # Disable DB for unit tests
    yield


@pytest.fixture
def app():
    """Create a fresh FastAPI application for testing."""
    # Clear any cached imports
    import importlib
    from app import main
    importlib.reload(main)
    return main.app


@pytest.fixture
def client(app) -> Generator[TestClient, None, None]:
    """Create a test client."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_s3():
    """Mock S3 client for testing."""
    with patch("app.s3.s3_client") as mock:
        s3_mock = MagicMock()
        mock.return_value = s3_mock
        yield s3_mock


@pytest.fixture
def mock_db():
    """Mock database operations for testing."""
    with patch("app.db.repositories.provisioning_repo") as prov_mock, \
         patch("app.db.repositories.device_connection_repo") as conn_mock:
        prov_mock.upsert.return_value = True
        conn_mock.log.return_value = True
        yield {"provisioning": prov_mock, "connection": conn_mock}


@pytest.fixture
def valid_enroll_payload() -> dict:
    """Return a valid enrollment payload."""
    return {
        "device_name": "matisse",
        "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
        "email": "user@example.com",
    }


@pytest.fixture
def mock_jwt_token():
    """Mock JWT token claims."""
    return {
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "preferred_username": "testuser",
        "name": "Test User",
        "realm_access": {"roles": ["user"]},
        "resource_access": {},
        "scope": "openid email profile",
        "exp": 9999999999,
        "iat": 1000000000,
    }


@pytest.fixture
def auth_headers(mock_jwt_token):
    """Return headers with mocked authentication."""
    # For unit tests, we mock the auth dependency
    return {"Authorization": "Bearer mock-token"}
