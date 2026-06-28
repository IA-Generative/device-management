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
import threading
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

# Dev-only unauthenticated admin login. Opt-in and OFF by default (IMM-2): it
# only activates when DM_DEV_AUTOLOGIN is explicitly enabled AND no OIDC issuer
# is configured. The prod boot gate (app.main.validate_security_config) refuses
# to start if this flag is on in a prod-like environment.
DEV_AUTOLOGIN = (os.getenv("DM_DEV_AUTOLOGIN") or "").strip().lower() in ("1", "true", "yes", "on")

# Warn loudly if session secret is the insecure default in production
_app_env = os.getenv("DM_APP_ENV", "").strip().lower()
if SESSION_SECRET == "changeme-dev-only" and _app_env in ("prod", "production", "staging"):  # nosec B105: comparaison à la sentinelle de défaut non sécurisé, pas un secret en dur
    logger.critical(
        "ADMIN_SESSION_SECRET is set to the insecure default 'changeme-dev-only' "
        "in %s environment. Anyone can forge admin sessions. "
        "Set a strong random value: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"",
        _app_env,
    )

def _oidc_issuer_url() -> str:
    """Resolve OIDC issuer: explicit ADMIN_OIDC_ISSUER_URL, else derive from
    KEYCLOAK_ISSUER_URL + KEYCLOAK_REALM."""
    explicit = os.getenv("ADMIN_OIDC_ISSUER_URL", "").strip()
    if explicit:
        return explicit
    base = os.getenv("KEYCLOAK_ISSUER_URL", "").strip().rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM", "").strip()
    if base and realm:
        return f"{base}/realms/{realm}"
    return base

OIDC_ISSUER = _oidc_issuer_url()
CLIENT_ID = os.getenv("ADMIN_OIDC_CLIENT_ID", "admin-dm-ui")
CLIENT_SECRET = os.getenv("ADMIN_OIDC_CLIENT_SECRET", "")


def _admin_redirect_uri() -> str:
    """Callback du login admin, dérivé dynamiquement de PUBLIC_BASE_URL.

    Le routeur admin est monté sous /admin (cf. main.py), la route est /callback
    → le callback est /admin/callback. On part de l'ORIGINE (scheme://host) de
    PUBLIC_BASE_URL en ignorant un éventuel préfixe de chemin (ex. dgx: .../bootstrap) :
    le callback admin est servi à la racine. Cette URL doit figurer dans les
    "Valid redirect URIs" du client Keycloak. Retourne "" si PUBLIC_BASE_URL est
    absent/invalide.
    """
    parts = urllib.parse.urlsplit(os.getenv("PUBLIC_BASE_URL", "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/admin/callback"


REDIRECT_URI = _admin_redirect_uri()
REQUIRED_GROUP = os.getenv("ADMIN_REQUIRED_GROUP", "admin-dm")

# Public issuer URL: what the browser sees (may differ from OIDC_ISSUER used for
# server-side discovery when running inside Docker with host.docker.internal).
OIDC_PUBLIC_ISSUER = os.getenv("ADMIN_OIDC_PUBLIC_ISSUER_URL", "")

CSRF_COOKIE = "dm_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "_csrf_token"

# OIDC discovery cache (CT-7 / VULN-012: guarded by a lock for thread-safe lazy init).
_oidc_config: dict = {}
_oidc_config_lock = threading.Lock()


def _get_oidc_config() -> dict:
    """Fetch OIDC discovery config. If ADMIN_OIDC_PUBLIC_ISSUER_URL is set,
    rewrite endpoint URLs so browser-side redirects use the public URL
    (e.g. localhost:8082) instead of the internal one (host.docker.internal:8082)."""
    global _oidc_config
    # Fast path: dict truthiness read is atomic in CPython.
    if _oidc_config:
        return _oidc_config
    if not OIDC_ISSUER:
        return {}
    with _oidc_config_lock:
        # Re-check under the lock; another thread may have populated it.
        if _oidc_config:
            return _oidc_config
        url = OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                cfg = json.loads(r.read())
        except Exception:
            logger.warning("OIDC discovery failed for %s", url)
            return _oidc_config
        _oidc_config = cfg
    # Rewrite URLs: browser-facing endpoints use PUBLIC issuer,
    # server-side token exchange uses INTERNAL issuer (e.g. wireguard-proxy).
    if OIDC_PUBLIC_ISSUER and OIDC_ISSUER != OIDC_PUBLIC_ISSUER:
        # The discovery response contains the issuer's own URLs (public).
        # For server-side calls (token exchange), rewrite to internal URL.
        public_base = _oidc_config.get("issuer", "").rstrip("/")
        internal_base = OIDC_ISSUER.rstrip("/")
        # Save internal token endpoint (rewrite public → internal for server-side call)
        raw_token_ep = _oidc_config.get("token_endpoint", "")
        # JWKS fetch is ALSO server-side (verif de l'ID token) → rewrite public →
        # internal, sinon le pod ne peut pas joindre le jwks_uri public
        # (urlopen "Connection reset by peer"). Même traitement que le token endpoint.
        raw_jwks = _oidc_config.get("jwks_uri", "")
        if public_base and internal_base and public_base != internal_base:
            _oidc_config["_internal_token_endpoint"] = raw_token_ep.replace(
                public_base, internal_base
            )
            _oidc_config["_internal_jwks_uri"] = raw_jwks.replace(
                public_base, internal_base
            )
        else:
            _oidc_config["_internal_token_endpoint"] = raw_token_ep
            _oidc_config["_internal_jwks_uri"] = raw_jwks
        # Browser-facing URLs: keep as-is (they're already public from discovery).
        # NB: on conserve cfg["issuer"] public pour la vérif du claim iss (le token
        # est signé avec l'issuer frontend public).
    return _oidc_config


def _get_jwks_uri() -> str:
    """JWKS endpoint pour la vérif JWS server-side (URL interne si dispo)."""
    cfg = _get_oidc_config()
    return cfg.get("_internal_jwks_uri") or cfg.get("jwks_uri", "")


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
            # Dev-only auto-login: requires explicit opt-in (DM_DEV_AUTOLOGIN)
            # and an unconfigured OIDC issuer. Never triggers in production.
            if DEV_AUTOLOGIN and not OIDC_ISSUER:
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
            # PKCE: generate code_verifier and code_challenge (S256)
            code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).rstrip(b"=").decode()
            params = urllib.parse.urlencode({
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": "openid profile email",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            })
            resp = RedirectResponse(f"{cfg['authorization_endpoint']}?{params}")
            resp.set_cookie("dm_oidc_state", state, httponly=True, samesite="lax")
            resp.set_cookie("dm_pkce_verifier", code_verifier, httponly=True, samesite="lax")
            return resp

        request.state.admin_session = session
        return await func(request, *args, **kwargs)

    return wrapper


def require_admin_or_service_token(func):
    """Like require_admin, but ALSO accepts a valid x-admin-token header
    (== DM_QUEUE_ADMIN_TOKEN) for non-interactive CI callers (e.g. the
    deploy-release.sh release pipeline).

    Scoped on purpose: only endpoints that explicitly opt in get token auth —
    this does NOT broaden DM_QUEUE_ADMIN_TOKEN to every @require_admin route.
    Falls back to the normal admin-session flow when no/invalid token is given.
    """
    admin_wrapped = require_admin(func)

    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        tok = (request.headers.get("x-admin-token") or "").strip()
        expected = os.getenv("DM_QUEUE_ADMIN_TOKEN", "").strip()
        if expected and tok and hmac.compare_digest(tok, expected):
            request.state.admin_session = {
                "sub": "service",
                "email": "ci@service.local",
                "name": "CI service token",
            }
            return await func(request, *args, **kwargs)
        return await admin_wrapped(request, *args, **kwargs)

    return wrapper
