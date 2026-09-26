"""Version générale par plugin (issue #40).

Revision ID: 005
Revises: 004
Create Date: 2026-09-26

Rejoue le bloc idempotent de db/schema.sql : ajoute plugins.general_version_id
et l'initialise une seule fois avec la dernière version publiée servable.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = 'plugins'
      AND column_name = 'general_version_id'
  ) THEN
    ALTER TABLE plugins ADD COLUMN general_version_id INT
      REFERENCES plugin_versions(id) ON DELETE SET NULL;
    UPDATE plugins p SET general_version_id = (
      SELECT pv.id FROM plugin_versions pv
      LEFT JOIN artifacts a ON a.id = pv.artifact_id
      WHERE pv.plugin_id = p.id AND pv.status = 'published'
        AND ((pv.distribution_mode = 'managed'
              AND a.s3_path IS NOT NULL AND a.s3_path <> '')
             OR (pv.distribution_mode IN ('download_link','store')
                 AND pv.download_url IS NOT NULL AND pv.download_url <> ''))
      ORDER BY pv.published_at DESC NULLS LAST, pv.id DESC
      LIMIT 1
    );
  END IF;
END $$;
""")


def downgrade() -> None:
    op.execute("ALTER TABLE plugins DROP COLUMN IF EXISTS general_version_id")
