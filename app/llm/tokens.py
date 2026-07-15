"""llmToken signé par client — même pattern que le telemetryKey existant.

Le token est minté au moment de bâtir la réponse /config pour un client dont la
paire X-Relay-Client/X-Relay-Key vient d'être VALIDÉE (la source de vérité
d'identité reste le relay client). Il porte le client_uuid + email et permet au
plugin figé d'authentifier ses appels LLM via ``Authorization: Bearer <token>``
(schéma authHeaderName/Prefix par défaut) si celui-ci ne rejoue pas les
en-têtes X-Relay-* sur le trafic LLM.

Format : ``payload_b64.sig_b64`` — HMAC-SHA256(DM_LLM_TOKEN_SIGNING_KEY).
Clé et TTL hot-reloadables (runtime_config). Sans clé configurée : pas de mint
(champ vide, comme le telemetryKey) et vérification en 503.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from .. import runtime_config
from ..services.crypto import b64url_decode, b64url_encode
from .errors import LlmProxyError

DEFAULT_TTL_SECONDS = 3600


def _signing_key() -> str:
    return str(runtime_config.cfg("DM_LLM_TOKEN_SIGNING_KEY", "") or "").strip()


def _ttl_seconds() -> int:
    try:
        ttl = int(runtime_config.cfg("DM_LLM_TOKEN_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_SECONDS
    return max(60, ttl)


def mint_llm_token(*, client_uuid: str, email: str) -> tuple[str, int | None]:
    """Mint un llmToken signé. Retourne ("", None) si aucune clé n'est configurée."""
    secret = _signing_key()
    if not secret:
        return "", None
    now = int(time.time())
    payload = {
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + _ttl_seconds(),
        "client_uuid": str(client_uuid or ""),
        "email": str(email or ""),
        "scope": "llm",
    }
    payload_b64 = b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_b64}.{b64url_encode(sig)}", int(payload["exp"])


def verify_llm_token(token: str) -> dict:
    """Vérifie signature + expiration + scope. Lève LlmProxyError (401/503)."""
    secret = _signing_key()
    if not secret:
        raise LlmProxyError(
            503, "LLM token verification key is not configured.", err_type="api_error"
        )
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        raise LlmProxyError(401, "Malformed llm token.", code="invalid_api_key") from None

    expected_sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
    ).digest()
    try:
        provided_sig = b64url_decode(sig_b64)
    except Exception:
        raise LlmProxyError(401, "Malformed llm token signature.", code="invalid_api_key") from None
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise LlmProxyError(401, "Invalid llm token signature.", code="invalid_api_key")

    try:
        payload = json.loads(b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        raise LlmProxyError(401, "Malformed llm token payload.", code="invalid_api_key") from None
    if not isinstance(payload, dict) or payload.get("scope") != "llm":
        raise LlmProxyError(401, "Invalid llm token payload.", code="invalid_api_key")
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise LlmProxyError(401, "Llm token expired.", code="invalid_api_key")
    return payload
