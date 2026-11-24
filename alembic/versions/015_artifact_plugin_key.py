"""Le plugin entre dans la clé d'unicité d'un artefact

Revision ID: 015
Revises: 014
Create Date: 2026-11-24

Issue #62, second temps. La migration 014 a rattaché chaque artefact à son plugin
(artifacts.plugin_id) et le code n'écrit plus par `ON CONFLICT (device_type,
platform_variant, version)`. La contrainte d'unicité peut donc porter le plugin :
deux plugins de même device_type publient chacun leur artefact pour un même numéro.

Ordre : nouvel index unique d'abord, ancienne contrainte retirée ensuite. Le
nouvel index ne peut pas échouer sur des données existantes : l'ancienne
contrainte, plus stricte, les garantissait déjà uniques. NULLS DISTINCT (le
défaut) : des artefacts sans propriétaire ne se gênent pas entre eux, comme les
variantes NULL déjà aujourd'hui.

Retour arrière : l'image de la migration 014 écrit sans nommer de contrainte et
reste compatible. Une image plus ancienne nomme l'ancienne contrainte dans son
ON CONFLICT et ne pourrait plus envoyer d'artefact. Le downgrade recrée
l'ancienne contrainte, ce qui échoue si deux plugins ont déjà publié le même
numéro : c'est voulu, il faut alors trancher à la main.

Idempotente, rejouable sur une base qui a déjà reçu db/schema.sql (qui porte les
mêmes ordres).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS artifacts_plugin_number_key
            ON artifacts (plugin_id, device_type, platform_variant, version)
    """)
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS "
               "artifacts_device_type_platform_variant_version_key")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'artifacts_device_type_platform_variant_version_key'
          ) THEN
            ALTER TABLE artifacts ADD CONSTRAINT artifacts_device_type_platform_variant_version_key
              UNIQUE (device_type, platform_variant, version);
          END IF;
        END $$;
    """)
    op.execute("DROP INDEX IF EXISTS artifacts_plugin_number_key")
