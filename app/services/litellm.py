"""LiteLLM virtual-key provisioning.

The DM holds a LiteLLM admin key and uses it to mint a scoped, per-device
virtual key at enrollment (and revoke the previous one on rotation). The admin
key never leaves the DM; only the minted per-device key is returned to the
device.
"""
from __future__ import annotations

import json
import logging
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger("device-management")


def resolve_admin_base_url(explicit: str, llm_base_url: str) -> str:
    """Return the LiteLLM management base URL.

    LiteLLM serves /key/generate at the proxy root, not under the OpenAI-compatible
    /v1 path, so when only LLM_BASE_URL (e.g. ``https://host/v1``) is known we strip
    a trailing /v1.
    """
    base = (explicit or "").strip().rstrip("/")
    if base:
        return base
    base = (llm_base_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base


def _post(admin_base_url: str, admin_key: str, path: str, body: dict, timeout: int) -> dict:
    url = f"{admin_base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_key}",
        },
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    if not payload:
        return {}
    parsed = json.loads(payload.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def generate_device_key(
    *,
    admin_base_url: str,
    admin_key: str,
    key_alias: str,
    duration_seconds: int,
    metadata: dict | None = None,
    timeout: int = 10,
) -> dict:
    """Mint a per-device LiteLLM key. Returns {"key": ..., "expires": ...}.

    Raises urllib errors on transport/HTTP failure; the caller decides whether to
    fail the enrollment or degrade gracefully.
    """
    body: dict = {
        "key_alias": key_alias,
        "duration": f"{int(duration_seconds)}s",
    }
    if metadata:
        body["metadata"] = metadata
    return _post(admin_base_url, admin_key, "/key/generate", body, timeout)


def delete_device_key(
    *,
    admin_base_url: str,
    admin_key: str,
    key_alias: str,
    timeout: int = 10,
) -> None:
    """Revoke a previously minted per-device key by alias. Best-effort."""
    try:
        _post(admin_base_url, admin_key, "/key/delete", {"key_aliases": [key_alias]}, timeout)
    except urllib_error.HTTPError as exc:
        # 404 / "not found" is fine — the key may already be gone or expired.
        logger.info("delete_device_key: %s for alias %s (ignored)", exc.code, key_alias)
    except Exception as exc:
        logger.warning("delete_device_key: failed for alias %s: %s", key_alias, exc)
