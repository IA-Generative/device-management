"""
Middleware module for device management.

Provides authentication and other cross-cutting concerns.
"""

from .auth import (
    JWTConfig,
    JWKSCache,
    TokenUser,
    decode_token,
    extract_identity_from_request,
    get_current_user,
    get_current_user_optional,
    get_jwt_config,
    http_bearer,
    require_client_role,
    require_role,
)

__all__ = [
    "JWTConfig",
    "JWKSCache",
    "TokenUser",
    "decode_token",
    "extract_identity_from_request",
    "get_current_user",
    "get_current_user_optional",
    "get_jwt_config",
    "http_bearer",
    "require_client_role",
    "require_role",
]
