"""
Health check endpoint.

Provides system health status following RFC 7807 Problem Details.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..db import PSYCOPG2_AVAILABLE, check_connection
from ..models import CheckStatus, HealthzResponse
from ..s3 import s3_client
from ..settings import settings

router = APIRouter(tags=["Health"])
logger = logging.getLogger("device-management.healthz")


def _ensure_dir(path: str) -> None:
    """Create directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


@router.get(
    "/healthz",
    response_model=HealthzResponse,
    responses={
        200: {
            "description": "Health check results",
            "content": {"application/problem+json": {}},
        }
    },
    summary="Health check endpoint",
    description="Checks connectivity to all dependencies (local storage, S3, database).",
)
def healthz() -> JSONResponse:
    """
    Check health of all dependencies.

    Returns RFC 7807 Problem Details format with individual check results.
    Always returns 200 to allow monitoring of degraded states.
    """
    errors: list[str] = []
    checks: dict[str, dict[str, str | None]] = {}

    # Check local storage
    if settings.store_enroll_locally:
        try:
            _ensure_dir(settings.enroll_dir)
            test_path = os.path.join(settings.enroll_dir, ".write_test")
            with open(test_path, "wb") as f:
                f.write(b"ok")
            os.remove(test_path)
            checks["local_storage"] = {"status": "ok", "detail": None}
        except Exception as e:
            errors.append(f"Local enroll_dir not writable: {e!r}")
            checks["local_storage"] = {"status": "error", "detail": str(e)}
    else:
        checks["local_storage"] = {"status": "skipped", "detail": None}

    # Check S3
    s3_required = settings.store_enroll_s3 or settings.binaries_mode in ("presign", "proxy")
    if s3_required and not settings.s3_bucket:
        errors.append("S3 bucket is not configured (DM_S3_BUCKET missing).")
        checks["s3"] = {"status": "error", "detail": "bucket missing"}
    elif settings.s3_bucket:
        try:
            s3 = s3_client()
            s3.head_bucket(Bucket=settings.s3_bucket)
            checks["s3"] = {"status": "ok", "detail": None}
        except Exception as e:
            errors.append(f"S3 not reachable or unauthorized: {e!r}")
            checks["s3"] = {"status": "error", "detail": str(e)}
    else:
        checks["s3"] = {"status": "skipped", "detail": None}

    # Check database
    if not PSYCOPG2_AVAILABLE:
        errors.append("psycopg2 is not installed; cannot verify DB connection.")
        checks["db"] = {"status": "error", "detail": "psycopg2 missing"}
    else:
        is_healthy, error_msg = check_connection()
        if is_healthy:
            checks["db"] = {"status": "ok", "detail": None}
        else:
            errors.append(f"DB not reachable or unauthorized: {error_msg}")
            checks["db"] = {"status": "error", "detail": error_msg}

    # Build response
    if errors:
        return JSONResponse(
            status_code=200,
            media_type="application/problem+json",
            content={
                "type": "https://example.com/problems/dependency-check",
                "title": "Dependency check failed",
                "status": 200,
                "detail": "One or more dependencies are not healthy.",
                "checks": checks,
                "errors": errors,
            },
        )

    return JSONResponse(
        status_code=200,
        media_type="application/problem+json",
        content={
            "type": "https://example.com/problems/dependency-check",
            "title": "OK",
            "status": 200,
            "detail": "All dependencies are healthy.",
            "checks": checks,
            "errors": [],
        },
    )
