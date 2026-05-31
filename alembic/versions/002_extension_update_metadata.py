"""DM-4: extension update metadata + 1:N version→artifacts link

Revision ID: 002
Revises: 001
Create Date: 2026-05-31

Additive only (ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS): safe to run
against an existing database, and reversible by dropping the added objects.

- plugins.extension_id / plugins.gecko_id : identité d'auto-update par plugin
  (appid gupdate XML / addon id Mozilla JSON), constantes.
- plugin_version_artifacts : lien 1:N version→artefacts pour porter plusieurs
  binaires par release (ex. .crx Chromium + .xpi Gecko) × cibles, désambiguïsés
  par platform_variant. plugin_versions.artifact_id reste l'artefact principal/legacy.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE plugins ADD COLUMN IF NOT EXISTS extension_id VARCHAR(64)")
    op.execute("ALTER TABLE plugins ADD COLUMN IF NOT EXISTS gecko_id VARCHAR(128)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_version_artifacts (
            id SERIAL PRIMARY KEY,
            plugin_version_id INT NOT NULL REFERENCES plugin_versions(id) ON DELETE CASCADE,
            artifact_id INT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
            platform_variant VARCHAR(50) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (plugin_version_id, platform_variant)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pva_version "
        "ON plugin_version_artifacts(plugin_version_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plugin_version_artifacts CASCADE")
    op.execute("ALTER TABLE plugins DROP COLUMN IF EXISTS gecko_id")
    op.execute("ALTER TABLE plugins DROP COLUMN IF EXISTS extension_id")
