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
import uuid
from typing import Any

logger = logging.getLogger("device-management")


def b64url_encode(raw: bytes) -> str:
    """Base64-URL encode without padding."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(raw: str) -> bytes:
    """Base64-URL decode with padding restoration."""
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


def hash_relay_secret(relay_client_id: str, relay_key: str, pepper: str = "") -> str:
    """SHA256 hash of relay credentials with pepper."""
    base = f"{relay_client_id}:{relay_key}:{pepper}".encode("utf-8")
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
    "tokenOWUI",
    "telemetryKey",
    "keycloak_client_secret",
    "keycloakClientSecret",
}
