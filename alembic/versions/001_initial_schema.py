"""Initial schema from db/schema.sql

Revision ID: 001
Revises: None
Create Date: 2026-04-11

This migration applies the full device-management schema. It uses
IF NOT EXISTS / CREATE OR REPLACE throughout, making it safe to run
against an existing database (idempotent).

The schema source of truth remains db/schema.sql. This migration
simply wraps it for Alembic tracking. Future schema changes should
be new migration files, not edits to this one.
"""
from typing import Sequence, Union
from pathlib import Path

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema_path = Path(__file__).resolve().parent.parent.parent / "db" / "schema.sql"
    if not schema_path.exists():
        # Fallback: look relative to alembic/versions/
        schema_path = Path(__file__).resolve().parent.parent.parent / "schema.sql"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"schema.sql not found at {schema_path}. "
            "Ensure db/schema.sql exists in the project root."
        )
    sql = schema_path.read_text(encoding="utf-8")
    # Execute each statement separately (Alembic/SQLAlchemy doesn't support
    # multi-statement execution in a single op.execute call)
    # Split on semicolons but preserve $$ blocks (PL/pgSQL functions)
    _execute_sql(sql)


def downgrade() -> None:
    # Destructive: drops all tables in reverse order
    # Only use in dev/test — never in production
    op.execute("DROP TABLE IF EXISTS admin_audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS device_telemetry_events CASCADE")
    op.execute("DROP TABLE IF EXISTS plugin_keycloak_clients CASCADE")
    op.execute("DROP TABLE IF EXISTS keycloak_clients CASCADE")
    op.execute("DROP TABLE IF EXISTS communication_acks CASCADE")
    op.execute("DROP TABLE IF EXISTS survey_responses CASCADE")
    op.execute("DROP TABLE IF EXISTS communications CASCADE")
    op.execute("DROP TABLE IF EXISTS feature_flag_overrides CASCADE")
    op.execute("DROP TABLE IF EXISTS feature_flags CASCADE")
    op.execute("DROP TABLE IF EXISTS cohort_members CASCADE")
    op.execute("DROP TABLE IF EXISTS cohorts CASCADE")
    op.execute("DROP TABLE IF EXISTS campaign_device_status CASCADE")
    op.execute("DROP TABLE IF EXISTS campaigns CASCADE")
    op.execute("DROP TABLE IF EXISTS plugin_installations CASCADE")
    op.execute("DROP TABLE IF EXISTS plugin_versions CASCADE")
    op.execute("DROP TABLE IF EXISTS artifacts CASCADE")
    op.execute("DROP TABLE IF EXISTS alias_access_log CASCADE")
    op.execute("DROP TABLE IF EXISTS plugin_waitlist CASCADE")
    op.execute("DROP TABLE IF EXISTS plugin_env_overrides CASCADE")
    op.execute("DROP TABLE IF EXISTS plugin_aliases CASCADE")
    op.execute("DROP TABLE IF EXISTS plugins CASCADE")
    op.execute("DROP TABLE IF EXISTS queue_job_dead_letters CASCADE")
    op.execute("DROP TABLE IF EXISTS queue_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS relay_clients CASCADE")
    op.execute("DROP TABLE IF EXISTS device_connections CASCADE")
    op.execute("DROP TABLE IF EXISTS provisioning CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")


def _execute_sql(sql: str) -> None:
    """Execute a multi-statement SQL string, handling $$ blocks correctly."""
    # Simple state machine: track whether we're inside a $$ block
    statements = []
    current = []
    in_dollar_block = False

    for line in sql.split("\n"):
        stripped = line.strip()
        # Skip empty lines and comments at top level
        if not stripped and not in_dollar_block:
            continue
        if stripped.startswith("--") and not in_dollar_block:
            continue

        # Track $$ blocks
        dollar_count = line.count("$$")
        if dollar_count % 2 == 1:
            in_dollar_block = not in_dollar_block

        current.append(line)

        # If we hit a semicolon at end of line and we're not in a $$ block
        if stripped.endswith(";") and not in_dollar_block:
            stmt = "\n".join(current).strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            current = []

    # Flush any remaining
    if current:
        stmt = "\n".join(current).strip()
        if stmt and stmt != ";":
            statements.append(stmt)

    for stmt in statements:
        op.execute(stmt)
