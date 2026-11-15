"""Clés d'API personnelles

Revision ID: 013
Revises: 012
Create Date: 2026-11-15

L'API du DM n'avait qu'un jeton de service partagé. api_keys porte des clés
créées par un administrateur pour une personne ou un automate : nom, titulaire,
portée (read / write), plugins éventuels, échéance, révocation, dernier usage.
Seul le SHA-256 du secret est stocké ; la clé n'est montrée qu'à sa création.

Additive only, idempotente, rejouable sur une base qui a déjà reçu db/schema.sql
(qui porte les mêmes ordres).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
    id BIGSERIAL PRIMARY KEY,
    key_id VARCHAR(12) NOT NULL UNIQUE,
    secret_hash CHAR(64) NOT NULL,
    label VARCHAR(100) NOT NULL,
    owner_email TEXT NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    plugins TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    last_used_ip TEXT,
    renewed_at TIMESTAMPTZ,
    renewed_by TEXT,
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT
)
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS renewed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS renewed_by TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS api_keys_owner_idx ON api_keys (owner_email)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS api_keys")
