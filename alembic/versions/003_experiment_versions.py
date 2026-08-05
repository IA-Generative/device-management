"""Experiment branches: coexisting campaigns + experimental catalogue versions

Revision ID: 003
Revises: 002
Create Date: 2026-07-25

Additive only (ADD COLUMN IF NOT EXISTS + CHECK widening): safe to run against an
existing database, reversible by dropping the added objects.

- campaigns.is_experiment / campaigns.priority : permet à des campagnes
  d'expérimentation de coexister avec le rollout général (non auto-complétées par
  lui) et de départager le cas où un device matche plusieurs campagnes actives.
- plugin_versions.tag / plugin_versions.hypotheses + statut 'experimental' :
  versions martyres/prototypes exposées au pull catalogue (téléchargement opt-in
  par tag), servables par version/tag précis mais jamais comme « latest main ».
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS is_experiment BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 0")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_camp_experiment "
        "ON campaigns(plugin_id) WHERE is_experiment = true AND status = 'active'"
    )

    op.execute("ALTER TABLE plugin_versions ADD COLUMN IF NOT EXISTS tag VARCHAR(80)")
    op.execute("ALTER TABLE plugin_versions ADD COLUMN IF NOT EXISTS hypotheses JSONB")
    # Widen the status CHECK to allow 'experimental' (idempotent).
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'plugin_versions_status_check'
              AND conrelid = 'plugin_versions'::regclass
              AND pg_get_constraintdef(oid) NOT LIKE '%experimental%'
          ) THEN
            ALTER TABLE plugin_versions DROP CONSTRAINT plugin_versions_status_check;
            ALTER TABLE plugin_versions ADD CONSTRAINT plugin_versions_status_check
              CHECK (status IN ('draft','published','deprecated','yanked','experimental'));
          END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pv_experimental "
        "ON plugin_versions(plugin_id, tag) WHERE status = 'experimental'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pv_experimental")
    # Revert experimental versions before narrowing the CHECK back.
    op.execute("UPDATE plugin_versions SET status = 'draft' WHERE status = 'experimental'")
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'plugin_versions_status_check'
              AND conrelid = 'plugin_versions'::regclass
          ) THEN
            ALTER TABLE plugin_versions DROP CONSTRAINT plugin_versions_status_check;
            ALTER TABLE plugin_versions ADD CONSTRAINT plugin_versions_status_check
              CHECK (status IN ('draft','published','deprecated','yanked'));
          END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE plugin_versions DROP COLUMN IF EXISTS hypotheses")
    op.execute("ALTER TABLE plugin_versions DROP COLUMN IF EXISTS tag")
    op.execute("DROP INDEX IF EXISTS idx_camp_experiment")
    op.execute("ALTER TABLE campaigns DROP COLUMN IF EXISTS priority")
    op.execute("ALTER TABLE campaigns DROP COLUMN IF EXISTS is_experiment")
