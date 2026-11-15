"""Signature et provenance d'un paquet, relevées à l'import

Revision ID: 009
Revises: 008
Create Date: 2026-11-15

ADR-0006 : chaque paquet importé garde l'enveloppe signée du paquet tel qu'il
est servi (artifacts.signature, jointe à la directive de mise à jour) et sa
provenance (artifacts.provenance : empreinte du paquet reçu, contrôle de la
signature d'origine, nature de la version, fichiers modifiés par le DM).

Additive only (ADD COLUMN IF NOT EXISTS), idempotente, rejouable sur une base qui a
déjà reçu db/schema.sql (qui porte les mêmes ALTER). NULL = paquet importé avant
ce contrôle, ou DM qui ne signe pas.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS signature JSONB")
    op.execute("ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS provenance JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE artifacts DROP COLUMN IF EXISTS provenance")
    op.execute("ALTER TABLE artifacts DROP COLUMN IF EXISTS signature")
