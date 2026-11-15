"""Une campagne peut demander le redémarrage du logiciel hôte après l'installation

Revision ID: 011
Revises: 010
Create Date: 2026-11-15

Une mise à jour installée à chaud laisse des restes de l'ancienne version jusqu'au
vrai redémarrage du logiciel hôte. campaigns.restart_required permet de le
demander au déploiement : la directive de mise à jour porte alors
`restart_required: true`, et le plugin propose à l'utilisateur de redémarrer
(« maintenant » ou « plus tard », jamais de force).

Additive only (ADD COLUMN IF NOT EXISTS), idempotente, rejouable sur une base qui a
déjà reçu db/schema.sql (qui porte le même ALTER). Défaut : non — la directive
est inchangée tant que personne ne le demande.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS restart_required BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE campaigns DROP COLUMN IF EXISTS restart_required")
