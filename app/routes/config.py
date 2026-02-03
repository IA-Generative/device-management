"""
Configuration endpoint.

Serves dynamic configuration for devices/plugins with environment variable substitution.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..db import device_connection_repo
from ..models import ConfigProfile, DeviceName, ErrorResponse
from ..settings import settings

router = APIRouter(prefix="/config", tags=["Configuration"])
logger = logging.getLogger("device-management.config")

# Supports env-var placeholders in config templates.
# Preferred syntax: ${{VARNAME}}
# Backward-compatible syntax: ${VARNAME}
_TEMPLATE_VAR_RE = re.compile(r"\$\{\{([A-Z0-9_]+)\}\}|\$\{([A-Z0-9_]+)\}")

# Allowed device names
DEVICE_ALLOWLIST = {d.value for d in DeviceName}

# Allowed profiles
PROFILE_ALLOWLIST = {p.value for p in ConfigProfile}


def _repo_root() -> str:
    """Get repository root path."""
    # app/routes/ is two levels below repo root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _load_config_template(profile: str, device: str | None = None) -> dict[str, Any]:
    """
    Load a config template JSON from `config/`.

    Resolution order (device-specific first when provided):
    - config/<device>/config.<profile>.json
    - config/<device>/config.json
    - config/config.<profile>.json
    - config/config.json

    Args:
        profile: Configuration profile (dev, prod, int)
        device: Optional device name

    Returns:
        Parsed JSON config as dict

    Raises:
        FileNotFoundError: If no config file found
    """
    base = os.path.join(_repo_root(), "config")
    candidates = []

    if device:
        candidates.extend([
            os.path.join(base, device, f"config.{profile}.json"),
            os.path.join(base, device, "config.json"),
        ])

    candidates.extend([
        os.path.join(base, f"config.{profile}.json"),
        os.path.join(base, "config.json"),
    ])

    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    raise FileNotFoundError("No config template found in ./config (expected config.json)")


def _substitute_env_in_str(value: str) -> str:
    """
    Replace environment variable placeholders in a string.

    Supports both ${{VARNAME}} (preferred) and ${VARNAME} (legacy) syntax.
    Missing variables are replaced with empty string.
    """
    def repl(m: re.Match[str]) -> str:
        # group(1) matches ${{VARNAME}}, group(2) matches ${VARNAME}
        var = m.group(1) or m.group(2)
        return os.getenv(var or "", "")

    return _TEMPLATE_VAR_RE.sub(repl, value)


def _substitute_env(obj: Any) -> Any:
    """Recursively substitute environment variables in any string values."""
    if isinstance(obj, dict):
        return {k: _substitute_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(v) for v in obj]
    if isinstance(obj, str):
        return _substitute_env_in_str(obj)
    return obj


def _apply_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """Apply targeted overrides from environment (extensible)."""
    # Currently a no-op, but provides hook for future overrides
    return cfg


def _get_config_response(profile: str | None, device: str | None) -> JSONResponse:
    """
    Internal function to get configuration.

    Args:
        profile: Optional profile override (dev, prod, int)
        device: Optional device name

    Returns:
        JSONResponse with config or error
    """
    # Determine profile
    prof = (profile or os.getenv("DM_CONFIG_PROFILE", "prod")).strip().lower()
    if prof not in PROFILE_ALLOWLIST:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=f"profile must be one of: {', '.join(sorted(PROFILE_ALLOWLIST))}"
            ).model_dump(),
        )

    # Validate device
    dev = (device or "").strip().lower()
    if dev and dev not in DEVICE_ALLOWLIST:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=f"device must be one of: {', '.join(sorted(DEVICE_ALLOWLIST))}"
            ).model_dump(),
        )

    # Load config template
    try:
        cfg = _load_config_template(prof, dev or None)
    except FileNotFoundError as e:
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(error=str(e)).model_dump(),
        )

    # Process config
    cfg = _substitute_env(cfg)
    cfg = _apply_overrides(cfg)
    cfg["enabled"] = bool(settings.config_enabled)

    # Log access (best effort)
    try:
        device_connection_repo.log(
            action="CONFIG_GET",
            email="system@local",
            client_uuid="00000000-0000-0000-0000-000000000000",
            encryption_key_fingerprint="none",
        )
    except Exception:
        logger.exception("Failed to log config access")

    return JSONResponse(content=cfg, headers={"Cache-Control": "no-store"})


@router.get(
    "/config.json",
    responses={
        200: {"description": "Configuration JSON"},
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Get configuration",
    description="Returns dynamic configuration with environment variable substitution.",
)
def get_config(
    profile: str | None = Query(
        default=None,
        description="Configuration profile (dev, prod, int)",
        examples=["dev", "prod"],
    ),
    device: str | None = Query(
        default=None,
        description="Device name for device-specific config",
        examples=["matisse", "libreoffice"],
    ),
) -> JSONResponse:
    """
    Get configuration JSON.

    The configuration is loaded from template files and processed:
    1. Load template from config/<device>/config.<profile>.json (with fallbacks)
    2. Substitute environment variables (${{VAR}} or ${VAR})
    3. Apply any overrides
    4. Add enabled flag from settings
    """
    return _get_config_response(profile, device)


@router.get(
    "/{device}/config.json",
    responses={
        200: {"description": "Device-specific configuration JSON"},
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Get device-specific configuration",
    description="Returns device-specific configuration with fallback to default.",
)
def get_device_config(
    device: str,
    profile: str | None = Query(
        default=None,
        description="Configuration profile (dev, prod, int)",
        examples=["dev", "prod"],
    ),
) -> JSONResponse:
    """Get device-specific configuration."""
    return _get_config_response(profile, device)
