"""
Binary files endpoint.

Serves binary files from S3 with presigned URLs or proxy streaming.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse

from ..db import device_connection_repo
from ..s3 import s3_client
from ..settings import settings

router = APIRouter(prefix="/binaries", tags=["Binaries"])
logger = logging.getLogger("device-management.binaries")

S3_BINARIES_PREFIX = settings.s3_prefix_binaries


def _log_binary_access(mode: str) -> None:
    """Log binary access (best effort)."""
    try:
        device_connection_repo.log(
            action="BINARY_GET",
            email="system@local",
            client_uuid="00000000-0000-0000-0000-000000000000",
            encryption_key_fingerprint="none",
        )
    except Exception:
        logger.exception("Failed to log binary access (%s)", mode)


@router.get(
    "/{path:path}",
    responses={
        302: {"description": "Redirect to presigned S3 URL (presign mode)"},
        200: {"description": "Binary file stream (proxy mode)"},
        404: {"description": "Binary not found"},
        500: {"description": "Server configuration error"},
    },
    summary="Get binary file",
    description="Serves binary files from S3. Mode depends on DM_BINARIES_MODE setting.",
)
def get_binary(path: str):
    """
    Get a binary file from S3.

    Behavior depends on DM_BINARIES_MODE:
    - presign: Returns 302 redirect to a time-limited presigned S3 URL
    - proxy: Streams the file through the API (client doesn't see S3)
    """
    if not settings.s3_bucket:
        raise HTTPException(
            status_code=500,
            detail="S3 bucket not configured (DM_S3_BUCKET).",
        )

    # Build S3 key
    key = f"{S3_BINARIES_PREFIX.rstrip('/')}/{path.lstrip('/')}"
    s3 = s3_client()

    if settings.binaries_mode == "presign":
        return _serve_presigned(s3, key)

    if settings.binaries_mode == "proxy":
        return _serve_proxy(s3, key)

    raise HTTPException(
        status_code=500,
        detail="Invalid DM_BINARIES_MODE (must be 'presign' or 'proxy').",
    )


def _serve_presigned(s3, key: str) -> RedirectResponse:
    """
    Generate presigned URL and redirect.

    Args:
        s3: Boto3 S3 client
        key: S3 object key

    Returns:
        RedirectResponse to presigned URL

    Raises:
        HTTPException: If presigning fails
    """
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=settings.presign_ttl_seconds,
        )
        _log_binary_access("presign")
        return RedirectResponse(url=url, status_code=302)
    except Exception as e:
        logger.error("Failed to presign URL for %s: %s", key, e)
        raise HTTPException(
            status_code=404,
            detail=f"Binary not found or cannot presign: {e!r}",
        )


def _serve_proxy(s3, key: str) -> StreamingResponse:
    """
    Stream file through the API.

    Args:
        s3: Boto3 S3 client
        key: S3 object key

    Returns:
        StreamingResponse with file content

    Raises:
        HTTPException: If file not found
    """
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket, Key=key)
        body_stream = obj["Body"]
        content_type = obj.get("ContentType") or "application/octet-stream"

        def iterfile():
            """Generator to stream file in chunks."""
            chunk_size = 1024 * 1024  # 1MB chunks
            for chunk in iter(lambda: body_stream.read(chunk_size), b""):
                yield chunk

        _log_binary_access("proxy")
        return StreamingResponse(iterfile(), media_type=content_type)

    except Exception as e:
        logger.error("Failed to get object %s: %s", key, e)
        raise HTTPException(
            status_code=404,
            detail=f"Binary not found: {e!r}",
        )
