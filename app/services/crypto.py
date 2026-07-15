"""Cryptographic helpers: base64url encoding, relay key hashing, telemetry tokens.

Extracted from app/main.py — pure functions with no side effects.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

logger = logging.getLogger("device-management")

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_FERNET = True
except Exception:  # pragma: no cover - cryptography is a transitive dep (PyJWT[crypto])
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore
    _HAS_FERNET = False


def b64url_encode(raw: bytes) -> str:
    """Base64-URL encode without padding."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(raw: str) -> bytes:
    """Base64-URL decode with padding restoration."""
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


def hash_relay_secret(relay_client_id: str, relay_key: str, pepper: str = "") -> str:
    """SHA256 hash of relay credentials with pepper."""
    base = f"{relay_client_id}:{relay_key}:{pepper}".encode()
    return hashlib.sha256(base).hexdigest()


def mint_telemetry_token(
    client_uuid: str,
    device_name: str,
    profile: str,
    signing_key: str,
    ttl_seconds: int = 300,
) -> str:
    """Create a signed JWT-like telemetry token."""
    now = int(time.time())
    payload = {
        "client_uuid": client_uuid,
        "device_name": device_name,
        "profile": profile,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    payload_raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(signing_key.encode("utf-8"), payload_raw, hashlib.sha256).digest()
    payload_b64 = b64url_encode(payload_raw)
    token = f"{payload_b64}.{b64url_encode(sig)}"
    return token


def verify_telemetry_token(token: str, signing_key: str) -> dict | None:
    """Verify a telemetry token. Returns claims dict or None."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        payload_raw = b64url_decode(payload_b64)
        sig = b64url_decode(sig_b64)
        expected_sig = hmac.new(
            signing_key.encode("utf-8"), payload_raw, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        claims = json.loads(payload_raw)
        if claims.get("exp", 0) < int(time.time()):
            return None
        return claims
    except Exception:
        return None


# Secret config keys that should be scrubbed without relay auth
SECRET_CONFIG_KEYS = {
    "llm_api_tokens",
    "llmToken",
    "tokenOWUI",
    "telemetryKey",
    "keycloak_client_secret",
    "keycloakClientSecret",
}

# Single source of truth for sensitive environment variables that must never be
# shown in clear on admin diagnostic pages (IMM-7 / VULN-016). Consolidates the
# previously divergent lists (router.secret_keys, router.secret_vars,
# crypto.SECRET_CONFIG_KEYS) and explicitly includes DM_RELAY_SECRET_PEPPER,
# which was absent from all of them.
SENSITIVE_ENV_VARS = frozenset({
    "ADMIN_SESSION_SECRET",
    "ADMIN_OIDC_CLIENT_SECRET",
    "DM_RELAY_SECRET_PEPPER",
    "DM_CONFIG_SECRET_KEY",
    "DM_RELAY_PROXY_SHARED_TOKEN",
    "DM_TELEMETRY_TOKEN_SIGNING_KEY",
    "DM_TELEMETRY_UPSTREAM_KEY",
    "DM_QUEUE_ADMIN_TOKEN",
    "LLM_API_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_URL",
    "DATABASE_ADMIN_URL",
    "POSTGRES_PASSWORD",
    "DB_ADMIN_PASSWORD",
    "TELEMETRY_KEY",
    "TELEMETRY_SALT",
})

# Config-key namespace lowercased for case-insensitive matching.
_SENSITIVE_CONFIG_KEYS = frozenset(k.lower() for k in SECRET_CONFIG_KEYS) | {"telemetry_salt"}

# Fallback substrings: any key containing one of these is treated as sensitive.
_SENSITIVE_SUBSTRINGS = (
    "secret", "token", "password", "passwd", "pepper",
    "credential", "private_key", "signing_key", "api_key", "apikey",
)


def is_sensitive_key(key: str) -> bool:
    """Return True if a config/env key holds a secret and must be masked."""
    if not key:
        return False
    k = key.strip()
    if k in SENSITIVE_ENV_VARS:
        return True
    kl = k.lower()
    if kl in _SENSITIVE_CONFIG_KEYS:
        return True
    return any(s in kl for s in _SENSITIVE_SUBSTRINGS)


def mask_secret(val: str) -> str:
    """Mask a secret without leaking any characters (VULN-016): show only length
    and a truncated SHA-256 fingerprint so two values can be compared safely."""
    if not val:
        return val
    digest = hashlib.sha256(val.encode("utf-8")).hexdigest()[:8]
    return f"*** ({len(val)} chars, sha256:{digest})"


# ── Reversible encryption for runtime-config secret overrides ────────────────
# Editable secrets (e.g. LLM_API_TOKEN) persisted in config_overrides must be
# encrypted at rest. We use Fernet (authenticated symmetric encryption) keyed by
# DM_CONFIG_SECRET_KEY. The env value can be any string: we derive a valid Fernet
# key from its SHA-256 so operators don't have to generate a base64 32-byte key.
_ENC_PREFIX = "enc:v1:"
_fernet_cache: tuple[str, object] | None = None


def _config_fernet():
    """Return a Fernet instance derived from DM_CONFIG_SECRET_KEY, or None.

    Cached on the (raw key) value so a runtime change of the env is picked up.
    """
    global _fernet_cache
    if not _HAS_FERNET:
        return None
    raw = os.getenv("DM_CONFIG_SECRET_KEY", "")
    if not raw:
        return None
    if _fernet_cache is not None and _fernet_cache[0] == raw:
        return _fernet_cache[1]
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    fernet = Fernet(key)
    _fernet_cache = (raw, fernet)
    return fernet


def secrets_encryption_available() -> bool:
    """True if editable secrets can be encrypted at rest (key + lib present)."""
    return _config_fernet() is not None


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret override value. Raises RuntimeError if unavailable."""
    fernet = _config_fernet()
    if fernet is None:
        raise RuntimeError(
            "secret encryption unavailable: set DM_CONFIG_SECRET_KEY "
            "(and ensure 'cryptography' is installed)"
        )
    token = fernet.encrypt((plaintext or "").encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored secret override. Values without the enc prefix are
    returned as-is (defensive: tolerates legacy/plaintext rows)."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    fernet = _config_fernet()
    if fernet is None:
        raise RuntimeError("cannot decrypt secret: DM_CONFIG_SECRET_KEY missing")
    token = stored[len(_ENC_PREFIX):].encode("ascii")
    try:
        return fernet.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("secret decryption failed (wrong DM_CONFIG_SECRET_KEY?)") from exc


def is_encrypted_secret(stored: str) -> bool:
    """True if the stored value is an enc:v1: ciphertext."""
    return bool(stored) and stored.startswith(_ENC_PREFIX)
