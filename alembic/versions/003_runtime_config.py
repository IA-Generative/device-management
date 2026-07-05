"""Runtime config overrides: config_state, config_overrides, config_pod_state

Revision ID: 003
Revises: 002
Create Date: 2026-06-28

Additive only (CREATE TABLE IF NOT EXISTS): safe to run against an existing
database, reversible by dropping the added objects.

- config_state       : single-row generation counter for cross-pod change detection.
- config_overrides   : per-key admin overrides taking precedence over the ENV baseline
                       (Fernet-encrypted when is_secret; JSON when value_type='list').
- config_pod_state   : per-pod enrollment + propagation generation + health metrics.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS config_state (
            id          BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            generation  BIGINT NOT NULL DEFAULT 1,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by  TEXT
        )
        """
    )
    op.execute(
        "INSERT INTO config_state (id, generation) VALUES (TRUE, 1) "
        "ON CONFLICT (id) DO NOTHING"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS config_overrides (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            value_type  TEXT NOT NULL CHECK (value_type IN ('bool','int','float','str','list')),
            is_secret   BOOLEAN NOT NULL DEFAULT FALSE,
            updated_by  TEXT,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS config_pod_state (
            pod_name           TEXT PRIMARY KEY,
            node_name          TEXT,
            runtime_mode       TEXT NOT NULL,
            pid                INT,
            pod_ip             TEXT,
            port               INT,
            applied_generation BIGINT NOT NULL DEFAULT 0,
            app_version        TEXT,
            restart_count      INT NOT NULL DEFAULT 0,
            rss_bytes          BIGINT,
            mem_limit_bytes    BIGINT,
            load1              REAL,
            cpu_count          INT,
            requests_total     BIGINT,
            started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cps_heartbeat "
        "ON config_pod_state(last_heartbeat_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS config_pod_state CASCADE")
    op.execute("DROP TABLE IF EXISTS config_overrides CASCADE")
    op.execute("DROP TABLE IF EXISTS config_state CASCADE")
