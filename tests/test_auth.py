"""
Tests for JWT authentication middleware.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


class TestTokenUser:
    """Tests for TokenUser class."""

    def test_from_claims_basic(self):
        """Test creating TokenUser from basic claims."""
        from app.middleware.auth import TokenUser
        
        claims = {
            "sub": "user-123",
            "email": "user@example.com",
            "email_verified": True,
            "preferred_username": "testuser",
            "name": "Test User",
            "scope": "openid email profile",
        }
        
        user = TokenUser.from_claims(claims)
        
        assert user.sub == "user-123"
        assert user.email == "user@example.com"
        assert user.email_verified is True
        assert user.preferred_username == "testuser"
        assert user.name == "Test User"

    def test_from_claims_with_roles(self):
        """Test creating TokenUser with roles."""
        from app.middleware.auth import TokenUser
        
        claims = {
            "sub": "user-123",
            "realm_access": {"roles": ["user", "admin"]},
            "resource_access": {
                "my-client": {"roles": ["editor"]},
            },
            "scope": "openid",
        }
        
        user = TokenUser.from_claims(claims)
        
        assert user.has_role("user")
        assert user.has_role("admin")
        assert not user.has_role("superadmin")
        assert user.has_client_role("my-client", "editor")
        assert not user.has_client_role("my-client", "viewer")

    def test_from_claims_with_timestamps(self):
        """Test creating TokenUser with exp/iat timestamps."""
        from app.middleware.auth import TokenUser
        
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "sub": "user-123",
            "exp": now + 3600,  # 1 hour from now
            "iat": now,
            "scope": "openid",
        }
        
        user = TokenUser.from_claims(claims)
        
        assert user.exp is not None
        assert user.iat is not None
        assert user.exp > user.iat


class TestJWTConfig:
    """Tests for JWTConfig class."""

    def test_from_env_defaults(self, monkeypatch):
        """Test JWTConfig with default values."""
        from app.middleware.auth import JWTConfig
        
        # Clear relevant env vars
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
        
        # Clear the lru_cache
        from app.middleware.auth import get_jwt_config
        get_jwt_config.cache_clear()
        
        config = JWTConfig.from_env()
        
        assert config.realm == "bootstrap"
        assert config.client_id == "device-management-plugin"
        assert "RS256" in config.algorithms

    def test_from_env_with_custom_values(self, monkeypatch):
        """Test JWTConfig with custom environment values."""
        from app.middleware.auth import JWTConfig, get_jwt_config
        
        monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.example.com/realms/test")
        monkeypatch.setenv("KEYCLOAK_REALM", "test-realm")
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test-client")
        
        # Clear cache
        get_jwt_config.cache_clear()
        
        config = JWTConfig.from_env()
        
        assert config.issuer_url == "https://keycloak.example.com/realms/test"
        assert config.realm == "test-realm"
        assert config.client_id == "test-client"

    def test_jwks_url(self):
        """Test JWKS URL generation."""
        from app.middleware.auth import JWTConfig
        
        config = JWTConfig(
            issuer_url="https://keycloak.example.com/realms/test",
            realm="test",
            client_id="test-client",
            algorithms=["RS256"],
            verify_exp=True,
            verify_aud=False,
            leeway_seconds=30,
        )
        
        assert config.jwks_url == "https://keycloak.example.com/realms/test/protocol/openid-connect/certs"

    def test_jwks_url_none_when_no_issuer(self):
        """Test JWKS URL is None when issuer not configured."""
        from app.middleware.auth import JWTConfig
        
        config = JWTConfig(
            issuer_url=None,
            realm="test",
            client_id="test-client",
            algorithms=["RS256"],
            verify_exp=True,
            verify_aud=False,
            leeway_seconds=30,
        )
        
        assert config.jwks_url is None


class TestJWKSCache:
    """Tests for JWKSCache class."""

    def test_cache_invalidation(self):
        """Test cache invalidation."""
        from app.middleware.auth import JWKSCache
        
        cache = JWKSCache(ttl_seconds=3600)
        cache._keys = {"keys": []}
        cache._fetched_at = datetime.now(timezone.utc)
        
        cache.invalidate()
        
        assert cache._keys is None
        assert cache._fetched_at is None


class TestExtractIdentity:
    """Tests for extract_identity_from_request function."""

    def test_extract_from_user(self):
        """Test extracting identity from authenticated user."""
        from app.middleware.auth import extract_identity_from_request, TokenUser
        
        # Create mock request
        request = MagicMock()
        request.headers = {}
        
        # Create user with email
        user = TokenUser(
            sub="user-123",
            email="user@example.com",
            email_verified=True,
            preferred_username="testuser",
            name="Test User",
            given_name=None,
            family_name=None,
            realm_roles=[],
            client_roles=[],
            scope="openid",
            exp=None,
            iat=None,
            raw_claims={},
        )
        
        email, client_uuid, fingerprint = extract_identity_from_request(
            request, user=user
        )
        
        assert email == "user@example.com"

    def test_extract_from_headers(self):
        """Test extracting identity from headers."""
        from app.middleware.auth import extract_identity_from_request
        
        request = MagicMock()
        request.headers = {
            "X-User-Email": "header@example.com",
            "X-Client-UUID": "client-uuid-123",
            "X-Encryption-Key-Fingerprint": "sha256:abc",
        }
        
        email, client_uuid, fingerprint = extract_identity_from_request(request)
        
        assert email == "header@example.com"
        assert client_uuid == "client-uuid-123"
        assert fingerprint == "sha256:abc"

    def test_extract_from_body(self):
        """Test extracting identity from request body."""
        from app.middleware.auth import extract_identity_from_request
        
        request = MagicMock()
        request.headers = {}
        
        body = {
            "email": "body@example.com",
            "plugin_uuid": "body-uuid-123",
        }
        
        email, client_uuid, fingerprint = extract_identity_from_request(
            request, body_obj=body
        )
        
        assert email == "body@example.com"
        assert client_uuid == "body-uuid-123"

    def test_extract_defaults(self):
        """Test default values when no identity found."""
        from app.middleware.auth import extract_identity_from_request
        
        request = MagicMock()
        request.headers = {}
        
        email, client_uuid, fingerprint = extract_identity_from_request(request)
        
        assert email == "unknown@local"
        assert client_uuid == "00000000-0000-0000-0000-000000000000"
        assert fingerprint == "unknown"
