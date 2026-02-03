"""
Device Management API - FastAPI Application.

This is the main entry point for the Device Management service.
Routes are organized in separate modules under app/routes/.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import uvicorn

from .db import (
    PSYCOPG2_AVAILABLE,
    close_pool,
    get_admin_url,
    get_bootstrap_url,
    init_pool,
    wait_for_database,
)
from .db.connection import _get_database_url, _with_db
from .routes import api_router
from .settings import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("device-management")


def create_app() -> FastAPI:
    """
    Application factory for FastAPI.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="Device Management API",
        version="0.2.0",
        description="API for device configuration, enrollment, and binary distribution.",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Configure CORS
    origins = [o.strip() for o in settings.allow_origins.split(",")] if settings.allow_origins else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins != ["*"] else ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*"],
    )

    # Include all routes
    app.include_router(api_router)

    # Register startup/shutdown events
    app.add_event_handler("startup", _on_startup)
    app.add_event_handler("shutdown", _on_shutdown)

    return app


async def _on_startup() -> None:
    """Application startup handler."""
    logger.info("Starting Device Management API...")

    # Initialize database
    await _init_database()

    # Initialize connection pool
    init_pool()

    logger.info("Device Management API started successfully")


async def _on_shutdown() -> None:
    """Application shutdown handler."""
    logger.info("Shutting down Device Management API...")

    # Close connection pool
    close_pool()

    logger.info("Device Management API shut down")


async def _init_database() -> None:
    """
    Initialize database schema if needed.

    This handles:
    1. Waiting for database availability
    2. Creating the 'dev' role (for local development)
    3. Creating the 'bootstrap' database
    4. Applying the schema
    5. Granting privileges to 'dev' role
    """
    if not PSYCOPG2_AVAILABLE:
        logger.warning("psycopg2 not installed; skipping DB bootstrap/schema init")
        return

    # Import psycopg2 here to avoid issues if not installed
    import psycopg2

    base_url = _get_database_url()
    if not base_url:
        logger.warning("No DATABASE_URL configured; skipping DB init")
        return

    # Get schema path
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    schema_path = os.path.join(repo_root, "infra-minimal", "db-schema.sql")

    if not os.path.isfile(schema_path):
        logger.warning("Schema file not found: %s", schema_path)
        return

    admin_url = get_admin_url(base_url)

    # Wait for database and setup
    try:
        if admin_url:
            postgres_url = _with_db(admin_url, "postgres")
            wait_for_database(postgres_url, timeout_seconds=30)

            # Create dev role (best effort)
            try:
                _ensure_dev_role(postgres_url)
            except psycopg2.Error:
                logger.warning("Skipping dev role creation (insufficient privilege)")

            # Create bootstrap database
            _ensure_database_exists(postgres_url, "bootstrap")

            # Apply schema
            bootstrap_url = _with_db(admin_url, "bootstrap")
            _apply_schema(bootstrap_url, schema_path)

            # Grant privileges
            _ensure_dev_privileges(bootstrap_url)

        else:
            # No admin URL, try with app credentials
            bootstrap_url = get_bootstrap_url()
            if bootstrap_url:
                wait_for_database(bootstrap_url, timeout_seconds=30)
                _apply_schema(bootstrap_url, schema_path)

    except Exception:
        logger.exception("Failed to initialize database")


def _ensure_dev_role(admin_url: str) -> None:
    """Create the 'dev' role if it doesn't exist."""
    import psycopg2

    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'dev'")
            if not cur.fetchone():
                cur.execute("CREATE ROLE dev LOGIN PASSWORD 'dev'")
            try:
                cur.execute(
                    "ALTER ROLE dev NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            except psycopg2.Error:
                pass  # Ignore if already set
    finally:
        conn.close()


def _ensure_database_exists(admin_url: str, db_name: str) -> None:
    """Create database if it doesn't exist."""
    import psycopg2

    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
                logger.info("Created database: %s", db_name)
    finally:
        conn.close()


def _apply_schema(db_url: str, schema_path: str) -> None:
    """Apply SQL schema to database."""
    import psycopg2

    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        logger.info("Schema applied successfully")
    finally:
        conn.close()


def _ensure_dev_privileges(admin_bootstrap_url: str) -> None:
    """Grant privileges to dev role."""
    import psycopg2

    conn = psycopg2.connect(admin_bootstrap_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("GRANT CONNECT ON DATABASE bootstrap TO dev")
            cur.execute("GRANT USAGE ON SCHEMA public TO dev")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dev")
            cur.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dev"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO dev"
            )
    finally:
        conn.close()


# Create the application instance
app = create_app()


# ---- Local entrypoint (VS Code / development friendly)
def _get_port() -> int:
    """Get port from environment."""
    try:
        return int(os.getenv("DM_PORT", os.getenv("PORT", "8000")))
    except ValueError:
        return 8000


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_get_port(),
        reload=os.getenv("RELOAD", "false").lower() in ("1", "true", "yes"),
        log_level=os.getenv("LOG_LEVEL", "info"),
    )
