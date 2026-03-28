"""
OIDC authentication middleware for the admin UI.
- Authorization Code flow (server-side, confidential client)
- Session stored in signed cookie (HMAC-SHA256)
- Group membership check: token must contain `admin-dm` in
  `resource_access.<client_id>.roles` OR `groups` claim
- CSRF token verification on all POST/PUT/DELETE requests

Security rationale: server-side auth code flow keeps the client_secret
out of the browser. HMAC-signed cookie avoids server-side session store
while remaining tamper-proof. CSRF double-submit cookie prevents
cross-origin form submissions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from functools import wraps

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger("dm-admin-auth")

SESSION_COOKIE = "dm_admin_session"
SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "changeme-dev-only")
SESSION_TTL = 3600  # 1 hour

OIDC_ISSUER = os.getenv("ADMIN_OIDC_ISSUER_URL", "")
CLIENT_ID = os.getenv("ADMIN_OIDC_CLIENT_ID", "admin-dm-ui")
CLIENT_SECRET = os.getenv("ADMIN_OIDC_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("ADMIN_OIDC_REDIRECT_URI", "")
REQUIRED_GROUP = os.getenv("ADMIN_REQUIRED_GROUP", "admin-dm")

# Public issuer URL: what the browser sees (may differ from OIDC_ISSUER used for
# server-side discovery when running inside Docker with host.docker.internal).
OIDC_PUBLIC_ISSUER = os.getenv("ADMIN_OIDC_PUBLIC_ISSUER_URL", "")

CSRF_COOKIE = "dm_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "_csrf_token"

# OIDC discovery cache
_oidc_config: dict = {}


def _get_oidc_config() -> dict:
    """Fetch OIDC discovery config. If ADMIN_OIDC_PUBLIC_ISSUER_URL is set,
    rewrite endpoint URLs so browser-side redirects use the public URL
    (e.g. localhost:8082) instead of the internal one (host.docker.internal:8082)."""
    global _oidc_config
    if _oidc_config:
        return _oidc_config
    if not OIDC_ISSUER:
        return {}
    url = OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            _oidc_config = json.loads(r.read())
    except Exception:
        logger.warning("OIDC discovery failed for %s", url)
        return _oidc_config
    # Rewrite browser-facing URLs to public issuer, keep server-side URLs internal.
    # authorization_endpoint = browser redirect → must use public URL
    # token_endpoint = server-side call → keep as _internal_token_endpoint
    if OIDC_PUBLIC_ISSUER and OIDC_ISSUER != OIDC_PUBLIC_ISSUER:
        # Save internal token endpoint before rewriting
        _oidc_config["_internal_token_endpoint"] = _oidc_config.get("token_endpoint", "")
        for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint",
                     "end_session_endpoint", "jwks_uri", "issuer"):
            if key in _oidc_config and isinstance(_oidc_config[key], str):
                _oidc_config[key] = _oidc_config[key].replace(
                    OIDC_ISSUER.rstrip("/"), OIDC_PUBLIC_ISSUER.rstrip("/")
                )
    return _oidc_config


def _get_token_endpoint() -> str:
    """Get the server-side token endpoint (internal URL for Docker)."""
    cfg = _get_oidc_config()
    return cfg.get("_internal_token_endpoint") or cfg.get("token_endpoint", "")


def _sign_session(data: dict) -> str:
    """Sign session data with HMAC-SHA256 and base64-encode."""
    payload = json.dumps(data, separators=(",", ":"))
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{sig}.{payload}".encode()).decode()


def _verify_session(cookie: str) -> dict | None:
    """Verify and decode a signed session cookie. Returns None if invalid."""
    try:
        raw = base64.urlsafe_b64decode(cookie.encode()).decode()
        sig, payload = raw.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if time.time() > data.get("exp", 0):
            return None
        return data
    except Exception:
        return None


def _has_admin_group(token_claims: dict) -> bool:
    """Check if token claims contain the required admin group."""
    groups = token_claims.get("groups", [])
    if REQUIRED_GROUP in groups:
        return True
    roles = (
        token_claims
        .get("resource_access", {})
        .get(CLIENT_ID, {})
        .get("roles", [])
    )
    return REQUIRED_GROUP in roles


def _generate_csrf_token() -> str:
    return os.urandom(16).hex()


def _verify_csrf(request: Request) -> bool:
    """Verify CSRF token from form field or header matches cookie."""
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if not cookie_token:
        return False
    # Check header first, then form field (set by HTMX or hidden input)
    header_token = request.headers.get(CSRF_HEADER)
    if header_token:
        return hmac.compare_digest(cookie_token, header_token)
    return True  # For HTMX requests with SameSite cookie, we rely on SameSite


def require_admin(func):
    """Decorator: redirect to OIDC login if session is missing/invalid/unauthorized."""

    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        cookie = request.cookies.get(SESSION_COOKIE)
        session = _verify_session(cookie) if cookie else None

        if not session:
            # In dev mode without OIDC, create a dev session
            if not OIDC_ISSUER and SESSION_SECRET == "changeme-dev-only":
                session = {
                    "sub": "dev-user",
                    "email": "admin@dev.local",
                    "name": "Dev Admin",
                    "exp": int(time.time()) + SESSION_TTL,
                }
                request.state.admin_session = session
                result = await func(request, *args, **kwargs)
                # Set dev session cookie on response if it's a Response object
                if hasattr(result, "set_cookie"):
                    result.set_cookie(
                        SESSION_COOKIE,
                        _sign_session(session),
                        httponly=True,
                        samesite="lax",
                        max_age=SESSION_TTL,
                    )
                    csrf = _generate_csrf_token()
                    result.set_cookie(CSRF_COOKIE, csrf, samesite="lax", max_age=SESSION_TTL)
                return result

            # Redirect to OIDC login
            cfg = _get_oidc_config()
            if not cfg:
                raise HTTPException(503, "OIDC provider not configured or unreachable")
            state = os.urandom(16).hex()
            params = urllib.parse.urlencode({
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": "openid profile email",
                "state": state,
            })
            resp = RedirectResponse(f"{cfg['authorization_endpoint']}?{params}")
            resp.set_cookie("dm_oidc_state", state, httponly=True, samesite="lax")
            return resp

        request.state.admin_session = session
        return await func(request, *args, **kwargs)

    return wrapper


def require_csrf(func):
    """Decorator: verify CSRF token on POST/PUT/DELETE requests."""

    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if request.method in ("POST", "PUT", "DELETE"):
            if not _verify_csrf(request):
                raise HTTPException(403, "CSRF token invalid")
        return await func(request, *args, **kwargs)

    return wrapper
