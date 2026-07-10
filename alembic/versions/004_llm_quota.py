"""Proxy LLM : compteurs de quota par utilisateur (fenêtre fixe)

Revision ID: 004
Revises: 003
Create Date: 2026-07-10

Additive only (CREATE TABLE IF NOT EXISTS): safe to run against an existing
database, reversible by dropping the added objects.

- llm_quota_counters : une ligne par (subject, fenêtre), créée lazy à la
  première requête via UPSERT atomique ; compteurs partagés entre réplicas.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_quota_counters (
            subject       TEXT        NOT NULL,
            window_start  TIMESTAMPTZ NOT NULL,
            count         INT         NOT NULL DEFAULT 0,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (subject, window_start)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_quota_window "
        "ON llm_quota_counters(window_start)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_quota_counters CASCADE")
