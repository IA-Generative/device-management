"""
Device enrollment endpoint.

Handles device registration and provisioning.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..db import device_connection_repo, provisioning_repo
from ..middleware import TokenUser, extract_identity_from_request, get_current_user_optional
from ..models import EnrollRequest, EnrollResponse, ErrorResponse
from ..s3 import s3_client
from ..settings import settings

router = APIRouter(tags=["Enrollment"])
logger = logging.getLogger("device-management.enroll")

MAX_BODY_BYTES = settings.max_body_size_mb * 1024 * 1024


def _ensure_dir(path: str) -> None:
    """Create directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


def _store_locally(body: bytes, filename: str) -> str:
    """
    Store enrollment payload to local filesystem.

    Args:
        body: Raw request body
        filename: Target filename

    Returns:
        Full path to stored file

    Raises:
        HTTPException: If storage fails
    """
    _ensure_dir(settings.enroll_dir)
    path = os.path.join(settings.enroll_dir, filename)

    try:
        with open(path, "wb") as f:
            f.write(body)
        return path
    except Exception as e:
        logger.error("Failed to write enrollment locally: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Cannot write local file: {e!r}",
        )


def _store_s3(body: bytes, filename: str) -> str:
    """
    Store enrollment payload to S3.

    Args:
        body: Raw request body
        filename: Target filename

    Returns:
        S3 URI of stored object

    Raises:
        HTTPException: If storage fails
    """
    if not settings.s3_bucket:
        raise HTTPException(
            status_code=500,
            detail="S3 bucket not configured (DM_S3_BUCKET).",
        )

    key = f"{settings.s3_prefix_enroll.rstrip('/')}/{filename}"

    try:
        s3 = s3_client()
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        return f"s3://{settings.s3_bucket}/{key}"
    except Exception as e:
        logger.error("Failed to write enrollment to S3: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Cannot write to S3: {e!r}",
        )


@router.api_route(
    "/enroll",
    methods=["POST", "PUT", "OPTIONS"],
    response_model=EnrollResponse,
    responses={
        201: {"model": EnrollResponse, "description": "Enrollment successful"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        413: {"model": ErrorResponse, "description": "Payload too large"},
        500: {"model": ErrorResponse, "description": "Server error"},
    },
    summary="Enroll a device",
    description="Register a device/plugin with the management service.",
)
async def enroll(
    request: Request,
    user: Annotated[TokenUser | None, Depends(get_current_user_optional)],
) -> Response:
    """
    Enroll a device/plugin.

    Accepts JSON payload with device_name, plugin_uuid, and email.
    Stores to local filesystem and/or S3 based on configuration.
    Records enrollment in database.

    Authentication is optional but recommended for production.
    """
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return Response(status_code=204)

    # Read and validate body size
    body = await request.body()
    if len(body) == 0:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error="Empty body").model_dump(),
        )
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content=ErrorResponse(error="Body too large").model_dump(),
        )

    # Parse JSON
    try:
        body_obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error=f"Body is not valid JSON: {e}").model_dump(),
        )

    if not isinstance(body_obj, dict):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error="Body must be a JSON object").model_dump(),
        )

    # Validate with Pydantic model
    try:
        enroll_request = EnrollRequest.model_validate(body_obj)
    except ValidationError as e:
        # Extract first error for user-friendly message
        errors = e.errors()
        if errors:
            first_error = errors[0]
            field = ".".join(str(loc) for loc in first_error.get("loc", []))
            msg = first_error.get("msg", "Invalid value")
            error_msg = f"{field}: {msg}" if field else msg
        else:
            error_msg = "Invalid request payload"

        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error=error_msg).model_dump(),
        )

    # Generate unique filename
    epoch_ms = int(time.time() * 1000)
    rid = uuid.uuid4().hex
    filename = f"{epoch_ms}-{rid}.json"

    # Storage results
    stored: dict[str, str] = {}

    # Store locally if enabled
    if settings.store_enroll_locally:
        local_path = _store_locally(body, filename)
        stored["local"] = local_path

    # Store to S3 if enabled
    if settings.store_enroll_s3:
        s3_uri = _store_s3(body, filename)
        stored["s3"] = s3_uri

    # Extract identity and update database
    try:
        email, client_uuid, fingerprint = extract_identity_from_request(
            request, user=user, body_obj=body_obj
        )

        # Prefer plugin_uuid from payload
        if enroll_request.plugin_uuid:
            client_uuid = str(enroll_request.plugin_uuid)

        # Use email from validated request
        email = enroll_request.email

        # Determine encryption key
        encryption_key = fingerprint if fingerprint and fingerprint != "unknown" else "unknown"

        # Upsert provisioning record
        provisioning_repo.upsert(
            email=email,
            client_uuid=client_uuid,
            device_name=enroll_request.device_name,
            encryption_key=encryption_key,
        )

        # Log connection
        device_connection_repo.log(
            action="ENROLL",
            email=email,
            client_uuid=client_uuid,
            encryption_key_fingerprint=fingerprint,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    except Exception:
        logger.exception("Failed to update database for enrollment")
        # Don't fail the request - storage was successful

    return JSONResponse(
        status_code=201,
        content=EnrollResponse(stored=stored).model_dump(),
    )
