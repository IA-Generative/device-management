"""Alembic environment configuration.

Runs migrations against the bootstrap database using raw SQL (no SQLAlchemy ORM).
Database URL is resolved from environment variables in this order:
  1. DATABASE_ADMIN_URL (admin credentials, can CREATE EXTENSION etc.)
  2. DATABASE_URL (app credentials)
"""
from __future__ import annotations

import os
import logging
from logging.config import fileConfig

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")


def _get_url() -> str:
    """Resolve database URL from environment."""
    url = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "No database URL found. Set DATABASE_URL or DATABASE_ADMIN_URL."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL without connecting)."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connects to the database)."""
    from sqlalchemy import create_engine

    url = _get_url()
    connectable = create_engine(url)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
