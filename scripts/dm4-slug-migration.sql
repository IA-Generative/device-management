-- DM-4d — Bascule slug mirai-browser -> iassistant-direct-browser (IDEMPOTENT)
--
-- Pré-requis :
--   1. Migration alembic 002 appliquée (colonnes plugins.extension_id/gecko_id,
--      table plugin_version_artifacts) — donc nouvelle image DM déployée.
--   2. Coordination avec la release client (bascule de la policy managée juste après).
--
-- Usage :
--   psql -v ON_ERROR_STOP=1 -d bootstrap -f scripts/dm4-slug-migration.sql
--
-- Rollback : voir le bloc commenté en fin de fichier.

BEGIN;

-- 1. Renommer le slug canonique (no-op si déjà fait ; ne fait rien si absent).
UPDATE plugins SET slug = 'iassistant-direct-browser', updated_at = now()
 WHERE slug = 'mirai-browser';

-- 2. Identité d'auto-update : Extension ID (constant) + gecko id.
UPDATE plugins
   SET extension_id = 'cjaokgcdmdeakhkplbifninjcdklhokf',
       gecko_id     = 'mirai-assistant@interieur.gouv.fr',
       updated_at   = now()
 WHERE slug = 'iassistant-direct-browser';

-- 3. Alias legacy -> même plugin (dual-serve config + updates pendant la transition).
INSERT INTO plugin_aliases (alias, plugin_id)
SELECT 'mirai-browser', id FROM plugins WHERE slug = 'iassistant-direct-browser'
ON CONFLICT (alias) DO UPDATE SET plugin_id = EXCLUDED.plugin_id;

-- 4. Override du client_id Keycloak pour l'extension (par profil), appliqué APRÈS
--    substitution ${{KEYCLOAK_CLIENT_ID}} -> gagne sur la valeur globale.
--    TODO(client) : confirmer le nom EXACT de la clé de config portant le client_id
--    dans le template de l'extension. Défaut 'keycloakClientId' (cf. _PLATFORM_DEFAULTS).
INSERT INTO plugin_env_overrides (plugin_id, environment, key, value, description)
SELECT p.id, e.env, 'keycloakClientId', 'mirai-extension',
       'Extension navigateur : client Keycloak dédié (DM-4)'
FROM plugins p
CROSS JOIN (VALUES ('dev'), ('int'), ('prod')) AS e(env)
WHERE p.slug = 'iassistant-direct-browser'
ON CONFLICT (plugin_id, environment, key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;

-- Vérifications post-bascule :
--   SELECT slug, extension_id, gecko_id FROM plugins WHERE slug='iassistant-direct-browser';
--   SELECT * FROM plugin_aliases WHERE alias='mirai-browser';
--   SELECT environment, key, value FROM plugin_env_overrides WHERE key='keycloakClientId';
--
-- Rollback (si besoin de revenir en arrière avant migration complète des postes) :
--   DELETE FROM plugin_env_overrides WHERE key='keycloakClientId'
--     AND plugin_id=(SELECT id FROM plugins WHERE slug='iassistant-direct-browser');
--   DELETE FROM plugin_aliases WHERE alias='mirai-browser';
--   UPDATE plugins SET slug='mirai-browser' WHERE slug='iassistant-direct-browser';
