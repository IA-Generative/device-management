"""
Database module for device management.

Provides connection management and repository pattern for data access.
"""

from .connection import (
    PSYCOPG2_AVAILABLE,
    check_connection,
    close_pool,
    get_admin_connection,
    get_admin_url,
    get_bootstrap_url,
    get_connection,
    init_pool,
    wait_for_database,
)
from .repositories import (
    DeviceConnectionRepository,
    ProvisioningRepository,
    device_connection_repo,
    provisioning_repo,
)

__all__ = [
    # Connection management
    "PSYCOPG2_AVAILABLE",
    "init_pool",
    "close_pool",
    "get_connection",
    "get_admin_connection",
    "get_bootstrap_url",
    "get_admin_url",
    "wait_for_database",
    "check_connection",
    # Repositories
    "ProvisioningRepository",
    "DeviceConnectionRepository",
    "provisioning_repo",
    "device_connection_repo",
]
