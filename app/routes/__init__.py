"""
Routes module for device management API.

Provides modular endpoint definitions organized by functionality.
"""

from fastapi import APIRouter

from .binaries import router as binaries_router
from .config import router as config_router
from .enroll import router as enroll_router
from .healthz import router as healthz_router

# Create a main router that includes all sub-routers
api_router = APIRouter()

# Include all route modules
api_router.include_router(healthz_router)
api_router.include_router(config_router)
api_router.include_router(enroll_router)
api_router.include_router(binaries_router)

__all__ = [
    "api_router",
    "binaries_router",
    "config_router",
    "enroll_router",
    "healthz_router",
]
