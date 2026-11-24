"""Un artefact appartient à un plugin

Revision ID: 014
Revises: 013
Create Date: 2026-11-24

Issue #62 : la clé d'un artefact était (device_type, platform_variant, version).
Deux plugins du catalogue de même device_type publiant le même numéro se
disputaient une seule ligne : le second était refusé, ou remplaçait le binaire du
premier sur confirmation. artifacts.plugin_id rattache l'artefact à son plugin.

Initialisation à l'ajout de la colonne, seulement quand le propriétaire est
certain (un seul plugin référence l'artefact, ou un seul plugin porte ce
device_type) ; les autres restent NULL et sont repris par le premier plugin qui
republie ce numéro.

Additive only : la contrainte d'unicité (device_type, platform_variant, version)
reste en place, pour que l'image précédente, qui la nomme dans son ON CONFLICT,
puisse encore écrire après un retour arrière. Idempotente, rejouable sur une base
qui a déjà reçu db/schema.sql (qui porte le même bloc).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADD_AND_BACKFILL = """
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = 'artifacts'
      AND column_name = 'plugin_id'
  ) THEN
    ALTER TABLE artifacts ADD COLUMN plugin_id INT
      REFERENCES plugins(id) ON DELETE SET NULL;
    WITH owners AS (
      SELECT artifact_id, plugin_id FROM plugin_versions
       WHERE artifact_id IS NOT NULL AND plugin_id IS NOT NULL
      UNION
      SELECT pva.artifact_id, pv.plugin_id
        FROM plugin_version_artifacts pva
        JOIN plugin_versions pv ON pv.id = pva.plugin_version_id
       WHERE pv.plugin_id IS NOT NULL
      UNION
      SELECT artifact_id, plugin_id FROM campaigns
       WHERE artifact_id IS NOT NULL AND plugin_id IS NOT NULL
      UNION
      SELECT rollback_artifact_id, plugin_id FROM campaigns
       WHERE rollback_artifact_id IS NOT NULL AND plugin_id IS NOT NULL
    ), single_owner AS (
      SELECT artifact_id, MIN(plugin_id) AS plugin_id
        FROM owners GROUP BY artifact_id
      HAVING COUNT(DISTINCT plugin_id) = 1
    )
    UPDATE artifacts a SET plugin_id = s.plugin_id
      FROM single_owner s WHERE a.id = s.artifact_id;
    UPDATE artifacts a SET plugin_id = (
      SELECT MIN(p.id) FROM plugins p WHERE p.device_type = a.device_type
    )
    WHERE a.plugin_id IS NULL
      AND (SELECT COUNT(*) FROM plugins p WHERE p.device_type = a.device_type) = 1;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_ADD_AND_BACKFILL)
    op.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_plugin ON artifacts (plugin_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_artifacts_plugin")
    op.execute("ALTER TABLE artifacts DROP COLUMN IF EXISTS plugin_id")
