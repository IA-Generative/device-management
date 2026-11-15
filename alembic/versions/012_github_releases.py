"""Catalogue : source GitHub d'un plugin et relevé de ses releases

Revision ID: 012
Revises: 011
Create Date: 2026-11-15

Issue #108 : un plugin peut déclarer d'où viennent ses paquets
(plugins.github_source : dépôt, composant release-please, motif de l'artefact,
canal). Le worker relève périodiquement les releases de ce dépôt dans
plugin_github_releases ; plugin_github_state garde, par plugin, la date et le
résultat du dernier essai, le compteur d'échecs qui espace les suivants, et la
demande « Vérifier maintenant » déposée par l'administration.

Le jeton d'accès n'est jamais en base : il vient de l'environnement.

Additive only (ADD COLUMN / CREATE TABLE IF NOT EXISTS), idempotente, rejouable
sur une base qui a déjà reçu db/schema.sql (qui porte les mêmes ordres).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE plugins ADD COLUMN IF NOT EXISTS github_source JSONB")
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_github_releases (
            id BIGSERIAL PRIMARY KEY,
            plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
            tag VARCHAR(200) NOT NULL,
            version VARCHAR(100) NOT NULL,
            prerelease BOOLEAN NOT NULL DEFAULT false,
            nature VARCHAR(20) NOT NULL DEFAULT 'other',
            asset_name TEXT,
            asset_api_url TEXT,
            signature_api_url TEXT,
            release_url TEXT,
            release_notes TEXT,
            published_at TIMESTAMPTZ,
            state VARCHAR(20) NOT NULL DEFAULT 'available'
                CHECK (state IN ('available','imported','refused','ignored')),
            reason TEXT,
            checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (plugin_id, tag)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_github_state (
            plugin_id INT PRIMARY KEY REFERENCES plugins(id) ON DELETE CASCADE,
            checked_at TIMESTAMPTZ,
            succeeded_at TIMESTAMPTZ,
            status VARCHAR(20),
            error_code VARCHAR(40),
            message TEXT,
            failures INT NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ,
            requested_at TIMESTAMPTZ
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plugin_github_state")
    op.execute("DROP TABLE IF EXISTS plugin_github_releases")
    op.execute("ALTER TABLE plugins DROP COLUMN IF EXISTS github_source")
