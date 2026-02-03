"""
Database connection management.

This module provides connection pooling and utilities for PostgreSQL access.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from psycopg2.extensions import connection as PgConnection

try:
    import psycopg2
    from psycopg2 import pool

    PSYCOPG2_AVAILABLE = True
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore
    pool = None  # type: ignore
    PSYCOPG2_AVAILABLE = False

logger = logging.getLogger("device-management.db")

# Connection pool (lazy initialized)
_connection_pool: pool.ThreadedConnectionPool | None = None


def _get_database_url() -> str | None:
    """Get database URL from environment."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    if os.getenv("RELOAD", "").lower() == "true":
        return "postgresql://dev:dev@localhost:5432/bootstrap"
    return None


def _with_db(url: str, db_name: str) -> str:
    """Replace the database name in a PostgreSQL URL."""
    parsed = urlparse(url)
    path = f"/{db_name}"
    return urlunparse(parsed._replace(path=path))


def get_bootstrap_url() -> str | None:
    """Get the URL for the bootstrap database."""
    base = _get_database_url()
    if not base:
        return None
    return _with_db(base, "bootstrap")


def get_admin_url(base_url: str | None = None) -> str | None:
    """
    Get the admin database URL for privileged operations.

    Priority:
    1. DATABASE_ADMIN_URL or DM_DATABASE_ADMIN_URL env var
    2. Constructed from POSTGRES_* env vars
    """
    explicit = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DM_DATABASE_ADMIN_URL")
    if explicit:
        return explicit

    base = base_url or _get_database_url()
    if not base:
        return None

    parsed = urlparse(base)
    admin_user = (
        os.getenv("DB_ADMIN_USER")
        or os.getenv("POSTGRES_ADMIN_USER")
        or os.getenv("POSTGRES_USER")
        or "postgres"
    )
    admin_password = (
        os.getenv("DB_ADMIN_PASSWORD")
        or os.getenv("POSTGRES_ADMIN_PASSWORD")
        or os.getenv("POSTGRES_PASSWORD")
    )

    if admin_password:
        netloc = f"{admin_user}:{admin_password}@{parsed.hostname}"
    else:
        netloc = f"{admin_user}@{parsed.hostname}"

    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse(parsed._replace(netloc=netloc))


def init_pool(min_conn: int = 1, max_conn: int = 10) -> None:
    """
    Initialize the connection pool.

    Should be called once at application startup.
    """
    global _connection_pool

    if not PSYCOPG2_AVAILABLE:
        logger.warning("psycopg2 not available, connection pool disabled")
        return

    db_url = get_bootstrap_url()
    if not db_url:
        logger.warning("No DATABASE_URL configured, connection pool disabled")
        return

    try:
        _connection_pool = pool.ThreadedConnectionPool(min_conn, max_conn, db_url)
        logger.info("Database connection pool initialized (min=%d, max=%d)", min_conn, max_conn)
    except Exception as e:
        logger.error("Failed to initialize connection pool: %s", e)
        _connection_pool = None


def close_pool() -> None:
    """Close the connection pool. Should be called at application shutdown."""
    global _connection_pool

    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("Database connection pool closed")


@contextmanager
def get_connection() -> Generator[PgConnection, None, None]:
    """
    Get a database connection from the pool.

    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    Raises:
        RuntimeError: If psycopg2 is not available or pool not initialized.
    """
    if not PSYCOPG2_AVAILABLE:
        raise RuntimeError(
            "psycopg2 is not installed. Install with: pip install psycopg2-binary"
        )

    # Use pool if available, otherwise create direct connection
    if _connection_pool:
        conn = _connection_pool.getconn()
        try:
            yield conn
        finally:
            _connection_pool.putconn(conn)
    else:
        # Fallback to direct connection (for tests or when pool not initialized)
        db_url = get_bootstrap_url()
        if not db_url:
            raise RuntimeError("No DATABASE_URL configured")
        conn = psycopg2.connect(db_url)
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def get_admin_connection(db_name: str = "postgres") -> Generator[PgConnection, None, None]:
    """
    Get an admin connection for privileged operations.

    Args:
        db_name: Database to connect to (default: postgres for DDL operations)
    """
    if not PSYCOPG2_AVAILABLE:
        raise RuntimeError(
            "psycopg2 is not installed. Install with: pip install psycopg2-binary"
        )

    admin_url = get_admin_url()
    if not admin_url:
        raise RuntimeError("No admin database URL available")

    admin_url = _with_db(admin_url, db_name)
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def wait_for_database(
    db_url: str | None = None,
    timeout_seconds: int = 30,
    interval_seconds: float = 1.0,
) -> None:
    """
    Wait for the database to become available.

    Args:
        db_url: Database URL (uses get_bootstrap_url() if not provided)
        timeout_seconds: Maximum time to wait
        interval_seconds: Time between connection attempts

    Raises:
        RuntimeError: If psycopg2 is not available
        psycopg2.OperationalError: If database not available after timeout
    """
    if not PSYCOPG2_AVAILABLE:
        raise RuntimeError(
            "psycopg2 is not installed. Install with: pip install psycopg2-binary"
        )

    url = db_url or get_bootstrap_url()
    if not url:
        raise RuntimeError("No database URL available")

    deadline = time.time() + timeout_seconds
    last_exc: Exception | None = None

    while time.time() < deadline:
        try:
            conn = psycopg2.connect(url, connect_timeout=3)
            conn.close()
            logger.info("Database connection established")
            return
        except psycopg2.OperationalError as exc:
            last_exc = exc
            logger.debug("Waiting for database... (%s)", exc)
            time.sleep(interval_seconds)

    if last_exc:
        raise last_exc


def check_connection() -> tuple[bool, str | None]:
    """
    Check if the database is reachable.

    Returns:
        Tuple of (is_healthy, error_message)
    """
    if not PSYCOPG2_AVAILABLE:
        return False, "psycopg2 not installed"

    db_url = get_bootstrap_url()
    if not db_url:
        return False, "DATABASE_URL not configured"

    try:
        conn = psycopg2.connect(db_url, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
            return True, None
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)
