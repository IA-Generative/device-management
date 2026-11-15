"""Liste de DM que porte un paquet, relevée à l'envoi

Revision ID: 008
Revises: 007
Create Date: 2026-11-14

Issue #133 : un paquet servi par un DM doit ramener ses postes vers ce DM. À
l'envoi, la liste de DM inscrite dans le paquet est lue, comparée à celle de
l'instance et réécrite si elle diffère ; artifacts.bootstrap_info garde le relevé
(liste envoyée, liste servie, réécriture) pour la fiche de la version.

Additive only (ADD COLUMN IF NOT EXISTS), idempotente, rejouable sur une base qui a
déjà reçu db/schema.sql (qui porte le même ALTER). NULL = paquet envoyé avant ce
contrôle.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS bootstrap_info JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE artifacts DROP COLUMN IF EXISTS bootstrap_info")
