"""
JWT Authentication middleware for Keycloak integration.

This module provides JWT token validation using Keycloak's public keys.
Supports both required and optional authentication modes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from jose import JWTError, jwt
    from jose.exceptions import ExpiredSignatureError, JWTClaimsError

    JOSE_AVAILABLE = True
except ImportError:
    JOSE_AVAILABLE = False
    JWTError = Exception  # type: ignore
    ExpiredSignatureError = Exception  # type: ignore
    JWTClaimsError = Exception  # type: ignore

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

logger = logging.getLogger("device-management.auth")

# Security scheme for OpenAPI documentation
http_bearer = HTTPBearer(auto_error=False)


@dataclass
class JWTConfig:
    """JWT validation configuration."""

    issuer_url: str | None
    realm: str | None
    client_id: str | None
    algorithms: list[str]
    verify_exp: bool
    verify_aud: bool
    leeway_seconds: int

    @classmethod
    def from_env(cls) -> JWTConfig:
        """Create config from environment variables."""
        return cls(
            issuer_url=os.getenv("KEYCLOAK_ISSUER_URL"),
            realm=os.getenv("KEYCLOAK_REALM", "bootstrap"),
            client_id=os.getenv("KEYCLOAK_CLIENT_ID", "device-management-plugin"),
            algorithms=os.getenv("JWT_ALGORITHMS", "RS256").split(","),
            verify_exp=os.getenv("JWT_VERIFY_EXP", "true").lower() == "true",
            verify_aud=os.getenv("JWT_VERIFY_AUD", "false").lower() == "true",
            leeway_seconds=int(os.getenv("JWT_LEEWAY_SECONDS", "30")),
        )

    @property
    def jwks_url(self) -> str | None:
        """Get the JWKS endpoint URL."""
        if not self.issuer_url:
            return None
        # Keycloak JWKS endpoint pattern
        base = self.issuer_url.rstrip("/")
        return f"{base}/protocol/openid-connect/certs"


@dataclass
class TokenUser:
    """Authenticated user information extracted from JWT."""

    sub: str  # Subject (user ID)
    email: str | None
    email_verified: bool
    preferred_username: str | None
    name: str | None
    given_name: str | None
    family_name: str | None
    realm_roles: list[str]
    client_roles: list[str]
    scope: str
    exp: datetime | None
    iat: datetime | None
    raw_claims: dict

    @classmethod
    def from_claims(cls, claims: dict) -> TokenUser:
        """Create TokenUser from JWT claims."""
        # Extract realm roles
        realm_access = claims.get("realm_access", {})
        realm_roles = realm_access.get("roles", [])

        # Extract client roles (resource_access)
        resource_access = claims.get("resource_access", {})
        client_roles: list[str] = []
        for resource, access in resource_access.items():
            for role in access.get("roles", []):
                client_roles.append(f"{resource}:{role}")

        # Parse timestamps
        exp = None
        if "exp" in claims:
            exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)

        iat = None
        if "iat" in claims:
            iat = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)

        return cls(
            sub=claims.get("sub", ""),
            email=claims.get("email"),
            email_verified=claims.get("email_verified", False),
            preferred_username=claims.get("preferred_username"),
            name=claims.get("name"),
            given_name=claims.get("given_name"),
            family_name=claims.get("family_name"),
            realm_roles=realm_roles,
            client_roles=client_roles,
            scope=claims.get("scope", ""),
            exp=exp,
            iat=iat,
            raw_claims=claims,
        )

    def has_role(self, role: str) -> bool:
        """Check if user has a specific realm role."""
        return role in self.realm_roles

    def has_client_role(self, client: str, role: str) -> bool:
        """Check if user has a specific client role."""
        return f"{client}:{role}" in self.client_roles


class JWKSCache:
    """
    Cache for JWKS (JSON Web Key Set) from Keycloak.

    Implements automatic refresh on key rotation.
    """

    def __init__(self, ttl_seconds: int = 3600):
        self._keys: dict | None = None
        self._fetched_at: datetime | None = None
        self._ttl = timedelta(seconds=ttl_seconds)

    def get_keys(self, jwks_url: str) -> dict:
        """
        Get JWKS keys, fetching from remote if needed.

        Args:
            jwks_url: URL to fetch JWKS from

        Returns:
            JWKS as dict

        Raises:
            RuntimeError: If keys cannot be fetched
        """
        now = datetime.now(timezone.utc)

        # Return cached keys if still valid
        if (
            self._keys is not None
            and self._fetched_at is not None
            and (now - self._fetched_at) < self._ttl
        ):
            return self._keys

        # Fetch fresh keys
        self._keys = self._fetch_jwks(jwks_url)
        self._fetched_at = now
        return self._keys

    def _fetch_jwks(self, jwks_url: str) -> dict:
        """Fetch JWKS from remote endpoint."""
        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed, cannot fetch JWKS")

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(jwks_url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error("Failed to fetch JWKS from %s: %s", jwks_url, e)
            raise RuntimeError(f"Failed to fetch JWKS: {e}") from e

    def invalidate(self) -> None:
        """Invalidate the cache, forcing a refresh on next access."""
        self._keys = None
        self._fetched_at = None


# Global JWKS cache instance
_jwks_cache = JWKSCache()


@lru_cache(maxsize=1)
def get_jwt_config() -> JWTConfig:
    """Get JWT configuration (cached)."""
    return JWTConfig.from_env()


def decode_token(token: str, config: JWTConfig | None = None) -> dict:
    """
    Decode and validate a JWT token.

    Args:
        token: JWT token string
        config: Optional JWT config (uses env config if not provided)

    Returns:
        Decoded token claims

    Raises:
        HTTPException: If token is invalid or expired
    """
    if not JOSE_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT validation not available (python-jose not installed)",
        )

    config = config or get_jwt_config()

    if not config.jwks_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT validation not configured (KEYCLOAK_ISSUER_URL missing)",
        )

    try:
        # Get JWKS keys
        jwks = _jwks_cache.get_keys(config.jwks_url)

        # Get the key ID from the token header
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # Find the matching key
        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

        if not rsa_key:
            # Key not found, try refreshing JWKS (key rotation)
            _jwks_cache.invalidate()
            jwks = _jwks_cache.get_keys(config.jwks_url)
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    rsa_key = key
                    break

        if not rsa_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unable to find appropriate key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Validate and decode
        options = {
            "verify_exp": config.verify_exp,
            "verify_aud": config.verify_aud,
            "leeway": config.leeway_seconds,
        }

        audience = config.client_id if config.verify_aud else None

        claims = jwt.decode(
            token,
            rsa_key,
            algorithms=config.algorithms,
            audience=audience,
            issuer=config.issuer_url,
            options=options,
        )

        return claims

    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTClaimsError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token claims: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
) -> TokenUser:
    """
    Dependency to get the current authenticated user.

    Requires a valid Bearer token in the Authorization header.

    Usage:
        @app.get("/protected")
        async def protected_route(user: TokenUser = Depends(get_current_user)):
            return {"user_id": user.sub}
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_token(credentials.credentials)
    return TokenUser.from_claims(claims)


async def get_current_user_optional(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
) -> TokenUser | None:
    """
    Dependency to optionally get the current user.

    Returns None if no token is provided (for endpoints that work
    both authenticated and unauthenticated).

    Usage:
        @app.get("/config")
        async def get_config(user: TokenUser | None = Depends(get_current_user_optional)):
            if user:
                # Authenticated request
                ...
            else:
                # Anonymous request
                ...
    """
    if credentials is None:
        return None

    try:
        claims = decode_token(credentials.credentials)
        return TokenUser.from_claims(claims)
    except HTTPException:
        # Token provided but invalid - return None for optional auth
        logger.debug("Invalid token provided for optional auth endpoint")
        return None


def require_role(role: str):
    """
    Dependency factory that requires a specific realm role.

    Usage:
        @app.get("/admin")
        async def admin_route(user: TokenUser = Depends(require_role("admin"))):
            return {"user": user.sub}
    """

    async def role_checker(user: TokenUser = Depends(get_current_user)) -> TokenUser:
        if not user.has_role(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required role: {role}",
            )
        return user

    return role_checker


def require_client_role(client: str, role: str):
    """
    Dependency factory that requires a specific client role.

    Usage:
        @app.get("/api/admin")
        async def api_admin(
            user: TokenUser = Depends(require_client_role("my-client", "admin"))
        ):
            return {"user": user.sub}
    """

    async def role_checker(user: TokenUser = Depends(get_current_user)) -> TokenUser:
        if not user.has_client_role(client, role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required client role: {client}:{role}",
            )
        return user

    return role_checker


def extract_identity_from_request(
    request: Request,
    user: TokenUser | None = None,
    body_obj: dict | None = None,
) -> tuple[str, str, str]:
    """
    Extract identity information from various sources.

    Priority:
    1. Authenticated user (JWT)
    2. Request headers (X-User-Email, X-Client-UUID)
    3. Request body (email, plugin_uuid)
    4. Defaults

    Args:
        request: FastAPI request object
        user: Optional authenticated user
        body_obj: Optional parsed request body

    Returns:
        Tuple of (email, client_uuid, fingerprint)
    """
    # Email
    if user and user.email:
        email = user.email
    elif request.headers.get("X-User-Email"):
        email = request.headers.get("X-User-Email", "")
    elif body_obj and body_obj.get("email"):
        email = str(body_obj.get("email", ""))
    else:
        email = "unknown@local"

    # Client UUID
    if request.headers.get("X-Client-UUID"):
        client_uuid = request.headers.get("X-Client-UUID", "")
    elif body_obj and (body_obj.get("client_uuid") or body_obj.get("plugin_uuid")):
        client_uuid = str(body_obj.get("client_uuid") or body_obj.get("plugin_uuid", ""))
    else:
        client_uuid = "00000000-0000-0000-0000-000000000000"

    # Fingerprint
    fingerprint = (
        request.headers.get("X-Encryption-Key-Fingerprint")
        or (body_obj or {}).get("encryption_key_fingerprint")
        or "unknown"
    )

    return email, client_uuid, fingerprint
