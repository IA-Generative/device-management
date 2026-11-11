"""Export parc : demande de cycle forcé relevée par le worker, pod dans le journal

Revision ID: 007
Revises: 006
Create Date: 2026-11-09

Vu en production le 2026-11-08 : tous les pods portaient la boucle d'export, et le
bouton « Exporter maintenant » envoyait un secret vide depuis l'admin. Désormais
seul le worker exporte ; l'admin dépose sa demande dans parc_export_etat.demande_forcee,
et chaque ligne du journal dit quel pod a fait le cycle.

Additive only (ADD COLUMN IF NOT EXISTS), idempotente, rejouable sur une base qui a
déjà reçu db/schema.sql (qui porte les mêmes ALTER).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE parc_export_etat ADD COLUMN IF NOT EXISTS demande_forcee TIMESTAMPTZ")
    op.execute("ALTER TABLE parc_export_journal ADD COLUMN IF NOT EXISTS pod VARCHAR(80)")


def downgrade() -> None:
    op.execute("ALTER TABLE parc_export_journal DROP COLUMN IF EXISTS pod")
    op.execute("ALTER TABLE parc_export_etat DROP COLUMN IF EXISTS demande_forcee")
