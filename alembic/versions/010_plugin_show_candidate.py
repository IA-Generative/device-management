"""Afficher la version candidate d'un plugin au catalogue public

Revision ID: 010
Revises: 009
Create Date: 2026-11-15

Une version candidate (X.Y.Z-rc.N, statut « experimental ») n'est visible au
catalogue que par son lien. plugins.show_candidate permet à l'administrateur de
l'annoncer à tous, dans un bloc « Préversion » distinct de la version générale.

Additive only (ADD COLUMN IF NOT EXISTS), idempotente, rejouable sur une base qui a
déjà reçu db/schema.sql (qui porte le même ALTER). Défaut : non — rien ne change à
l'affichage tant que l'administrateur ne l'active pas.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE plugins ADD COLUMN IF NOT EXISTS show_candidate BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE plugins DROP COLUMN IF EXISTS show_candidate")
