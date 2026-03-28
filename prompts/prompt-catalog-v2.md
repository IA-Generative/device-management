# Prompt — Catalogue v2

> Version : 3.0 — 2026-03-28
> Perimetre : device-management
> Plugins : **mirai-libreoffice** (alias: libreoffice), **mirai-matisse** (alias: matisse)

---

## Instructions pour le coding agent

**Avant de coder** : lis ce prompt en entier. Chaque section est autonome et numerotee.
Implemente dans l'ordre de la section 12 (ordre d'implementation). Chaque etape doit
etre testable independamment. Ne passe a l'etape suivante que si la precedente fonctionne.

**Regles** :
- Ne modifie pas le contrat d'interface avec les plugins (meme URLs, meme format JSON)
- Les templates config sur disque (`config/libreoffice/`, `config/matisse/`) restent organises par `device_type`
- Tout le SQL est dans un seul fichier `db/schema.sql` (pas de migrations incrementales)
- Les services admin (DB) sont dans `app/admin/services/` — le router ne fait jamais de SQL directement
- Les pages publiques utilisent le DSFR via CDN, les pages admin utilisent `dm-admin.css`
- Le CSP doit autoriser `cdn.jsdelivr.net` (styles, scripts, fonts) pour les pages admin et publiques
- Chaque endpoint LLM affiche un spinner, pre-remplit le champ, et laisse l'admin modifier — le LLM ne publie jamais directement

**Fichiers cles a lire avant de commencer** :
- `app/main.py` — API principale, `get_config()`, `_apply_overrides()`
- `app/admin/router.py` — toutes les routes admin
- `app/admin/templates/base.html` — layout admin (nav, header)
- `app/admin/static/dm-admin.css` — styles admin
- `db/schema.sql` — schema actuel
- `config/libreoffice/config.dev.json` — exemple de template config

---

## Principe

```
device_name  = slug = identifiant universel    ex: "mirai-libreoffice"
device_type  = type interne (template config)  ex: "libreoffice"
alias        = retrocompatibilite              ex: "libreoffice" → "mirai-libreoffice"
```

---

## 1. Pipeline de configuration

Quand un plugin appelle `/config/{x}/config.json?profile=dev` :

```
 1. RESOLVE      x → (device_name, device_type, plugin_id, resolved_via)
                 slug exact → match | alias → lookup + LOG | inconnu → 400
 2. TEMPLATE     charge config/{device_type}/config.{profile}.json
 3. SUBSTITUTION ${{VAR}} → valeurs env systeme
 4. OVERRIDES DM telemetrie, relay, etc. (existant, inchange)
 5. OVERRIDES CATALOGUE  plugin_env_overrides WHERE plugin_id AND environment
 6. KEYCLOAK     plugin_keycloak_clients → injecte client_id + realm
 7. ACCESS CTRL  open | keycloak_group (JWT groups) | waitlist (approved)
 8. INJECTION    force device_name + config_path (meme si appel via alias)
 9. SCRUB        secrets masques si pas de relay credentials
10. ENRICHMENT   campaigns, features, communications (existant)
```

### Fonctions a implementer dans `app/main.py`

```python
def _resolve_device(device: str, cur) -> tuple[str|None, str|None, int|None, str]:
    """Resolve slug/alias → (device_name, device_type, plugin_id, resolved_via).
    resolved_via: 'slug' | 'alias' | 'unknown'
    """
    cur.execute("SELECT slug, device_type, id FROM plugins WHERE slug=%s AND status='active'", (device,))
    row = cur.fetchone()
    if row: return row[0], row[1], row[2], "slug"
    cur.execute("""SELECT p.slug, p.device_type, p.id FROM plugin_aliases a
                   JOIN plugins p ON p.id=a.plugin_id WHERE a.alias=%s AND p.status='active'""", (device,))
    row = cur.fetchone()
    if row: return row[0], row[1], row[2], "alias"
    return None, None, None, "unknown"

def _log_alias_access(cur, *, alias, slug, plugin_id, client_uuid="", source_ip=None):
    cur.execute("INSERT INTO alias_access_log(alias,slug,plugin_id,client_uuid,source_ip) VALUES(%s,%s,%s,%s,%s::inet)",
                (alias, slug, plugin_id, client_uuid or None, source_ip))

def _apply_catalog_overrides(cfg, *, plugin_id, profile, cur):
    config_obj = cfg.get("config")
    if not isinstance(config_obj, dict): return cfg
    cur.execute("SELECT key,value FROM plugin_env_overrides WHERE plugin_id=%s AND environment=%s", (plugin_id, profile))
    for key, value in cur.fetchall(): config_obj[key] = value
    cur.execute("""SELECT kc.client_id, kc.realm FROM plugin_keycloak_clients pkc
                   JOIN keycloak_clients kc ON kc.id=pkc.keycloak_client_id
                   WHERE pkc.plugin_id=%s AND pkc.environment=%s LIMIT 1""", (plugin_id, profile))
    kc = cur.fetchone()
    if kc: config_obj["keycloakClientId"] = kc[0]; config_obj["keycloakRealm"] = kc[1]
    return cfg
```

Injection device_name (etape 8) — apres les overrides, avant le return :
```python
if isinstance(config_obj, dict) and device_name:
    config_obj["device_name"] = device_name
    config_obj["config_path"] = f"/config/{device_name}/config.json"
```

---

## 2. Schema DB (fresh start)

Remplacer `db/schema.sql` + supprimer `db/migrations/`. Un seul fichier contenant toutes les tables.

<details>
<summary>Schema SQL complet (cliquer pour derouler)</summary>

```sql
-- Device Management — Schema v2 (fresh start)
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dev') THEN CREATE ROLE dev LOGIN PASSWORD 'dev'; END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at=now(); RETURN NEW; END; $$ LANGUAGE plpgsql;

-- CORE
CREATE TABLE IF NOT EXISTS provisioning (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), email CITEXT NOT NULL, device_name TEXT NOT NULL,
  client_uuid UUID NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING','ENROLLED','REVOKED','FAILED')),
  encryption_key TEXT NOT NULL, comments TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prov_active ON provisioning(client_uuid) WHERE status IN ('PENDING','ENROLLED');

CREATE TABLE IF NOT EXISTS device_connections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  email CITEXT NOT NULL, client_uuid UUID NOT NULL, action TEXT NOT NULL DEFAULT 'UNKNOWN',
  encryption_key_fingerprint TEXT NOT NULL, connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_ip INET, user_agent TEXT);
CREATE INDEX IF NOT EXISTS idx_dc_client ON device_connections(client_uuid, connected_at DESC);

CREATE TABLE IF NOT EXISTS relay_clients (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), client_uuid UUID NOT NULL, email CITEXT NOT NULL,
  relay_client_id TEXT NOT NULL, relay_key_hash TEXT NOT NULL,
  allowed_targets TEXT[] NOT NULL DEFAULT ARRAY['keycloak']::text[],
  expires_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, comments TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_relay_active ON relay_clients(client_uuid) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS queue_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), topic TEXT NOT NULL, payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','dead')),
  attempts INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 8,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), locked_at TIMESTAMPTZ, lock_owner TEXT,
  dedupe_key TEXT, completed_at TIMESTAMPTZ, last_error TEXT);
CREATE INDEX IF NOT EXISTS idx_qj_poll ON queue_jobs(status, next_attempt_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qj_dedupe ON queue_jobs(topic, dedupe_key);

CREATE TABLE IF NOT EXISTS queue_job_dead_letters (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  job_id UUID NOT NULL, topic TEXT NOT NULL, payload JSONB NOT NULL, dedupe_key TEXT,
  attempts INT NOT NULL, max_attempts INT NOT NULL, last_error TEXT);

-- CATALOGUE
CREATE TABLE IF NOT EXISTS plugins (
  id SERIAL PRIMARY KEY, slug VARCHAR(100) UNIQUE NOT NULL, name VARCHAR(200) NOT NULL,
  description TEXT, intent TEXT, key_features JSONB DEFAULT '[]'::jsonb, changelog TEXT,
  icon_url TEXT, icon_path TEXT, source_url TEXT, device_type VARCHAR(50) NOT NULL,
  category VARCHAR(100) DEFAULT 'productivity', homepage_url TEXT, support_email TEXT,
  publisher VARCHAR(200) DEFAULT 'DNUM',
  visibility VARCHAR(20) DEFAULT 'public' CHECK (visibility IN ('public','internal','hidden')),
  status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('draft','active','deprecated','removed')),
  maturity VARCHAR(20) DEFAULT 'release' CHECK (maturity IN ('dev','alpha','beta','pre-release','release')),
  access_mode VARCHAR(20) DEFAULT 'open' CHECK (access_mode IN ('open','waitlist','keycloak_group')),
  required_group VARCHAR(200),
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE IF NOT EXISTS plugin_aliases (
  alias VARCHAR(100) PRIMARY KEY, plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE);

CREATE TABLE IF NOT EXISTS plugin_env_overrides (
  id SERIAL PRIMARY KEY, plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
  environment VARCHAR(20) NOT NULL CHECK (environment IN ('dev','int','prod')),
  key VARCHAR(200) NOT NULL, value TEXT NOT NULL, is_secret BOOLEAN DEFAULT false, description TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (plugin_id, environment, key));

CREATE TABLE IF NOT EXISTS plugin_waitlist (
  id SERIAL PRIMARY KEY, plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
  email VARCHAR(255) NOT NULL, client_uuid VARCHAR(255), reason TEXT,
  status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  reviewed_by VARCHAR(255), reviewed_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (plugin_id, email));

CREATE TABLE IF NOT EXISTS alias_access_log (
  id BIGSERIAL PRIMARY KEY, alias VARCHAR(100) NOT NULL, slug VARCHAR(100) NOT NULL,
  plugin_id INT NOT NULL, client_uuid VARCHAR(255), source_ip INET,
  accessed_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_aal ON alias_access_log(alias, accessed_at DESC);

-- ARTIFACTS & VERSIONS
CREATE TABLE IF NOT EXISTS artifacts (
  id SERIAL PRIMARY KEY, device_type VARCHAR(50) NOT NULL, platform_variant VARCHAR(50),
  version VARCHAR(50) NOT NULL, s3_path TEXT, checksum VARCHAR(128),
  min_host_version VARCHAR(50), max_host_version VARCHAR(50), changelog_url TEXT,
  is_active BOOLEAN DEFAULT true, released_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (device_type, platform_variant, version));

CREATE TABLE IF NOT EXISTS plugin_versions (
  id SERIAL PRIMARY KEY, plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
  artifact_id INT REFERENCES artifacts(id), version VARCHAR(50) NOT NULL, release_notes TEXT,
  download_url TEXT, min_host_version VARCHAR(50), max_host_version VARCHAR(50),
  status VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft','published','deprecated','yanked')),
  distribution_mode VARCHAR(20) DEFAULT 'managed' CHECK (distribution_mode IN ('managed','download_link','store','manual')),
  published_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(), UNIQUE (plugin_id, version));

CREATE TABLE IF NOT EXISTS plugin_installations (
  id SERIAL PRIMARY KEY, plugin_id INT NOT NULL REFERENCES plugins(id),
  client_uuid VARCHAR(255) NOT NULL, email VARCHAR(255), installed_version VARCHAR(50),
  installed_at TIMESTAMPTZ DEFAULT now(), last_seen_at TIMESTAMPTZ DEFAULT now(),
  status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active','inactive','uninstalled')),
  UNIQUE (plugin_id, client_uuid));

-- CAMPAGNES & COHORTES
CREATE TABLE IF NOT EXISTS cohorts (
  id SERIAL PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL, description TEXT,
  type VARCHAR(20) NOT NULL CHECK (type IN ('manual','percentage','email_pattern','keycloak_group')),
  config JSONB, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE IF NOT EXISTS cohort_members (
  cohort_id INT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
  identifier_type VARCHAR(20) NOT NULL CHECK (identifier_type IN ('email','client_uuid')),
  identifier_value VARCHAR(255) NOT NULL, added_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (cohort_id, identifier_type, identifier_value));

CREATE TABLE IF NOT EXISTS campaigns (
  id SERIAL PRIMARY KEY, name VARCHAR(200) NOT NULL, description TEXT,
  type VARCHAR(30) DEFAULT 'plugin_update' CHECK (type IN ('plugin_update','config_patch','feature_set')),
  status VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft','active','paused','completed','rolled_back')),
  target_cohort_id INT REFERENCES cohorts(id), artifact_id INT REFERENCES artifacts(id),
  rollback_artifact_id INT REFERENCES artifacts(id), rollout_config JSONB,
  urgency VARCHAR(10) DEFAULT 'normal' CHECK (urgency IN ('low','normal','critical')),
  deadline_at TIMESTAMPTZ, created_by VARCHAR(255),
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE IF NOT EXISTS campaign_device_status (
  campaign_id INT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  client_uuid VARCHAR(255) NOT NULL, email VARCHAR(255),
  status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','notified','updated','failed','rolled_back')),
  version_before VARCHAR(50), version_after VARCHAR(50), error_message TEXT,
  last_contact_at TIMESTAMPTZ, updated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (campaign_id, client_uuid));

CREATE TABLE IF NOT EXISTS feature_flags (
  id SERIAL PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL, description TEXT,
  default_value BOOLEAN DEFAULT true, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE IF NOT EXISTS feature_flag_overrides (
  feature_id INT NOT NULL REFERENCES feature_flags(id) ON DELETE CASCADE,
  cohort_id INT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
  value BOOLEAN NOT NULL, min_plugin_version VARCHAR(50), PRIMARY KEY (feature_id, cohort_id));

-- COMMUNICATIONS
CREATE TABLE IF NOT EXISTS communications (
  id SERIAL PRIMARY KEY,
  type VARCHAR(20) NOT NULL CHECK (type IN ('announcement','alert','survey','changelog')),
  title VARCHAR(300) NOT NULL, body TEXT NOT NULL,
  priority VARCHAR(10) DEFAULT 'normal' CHECK (priority IN ('low','normal','high','critical')),
  target_plugin_id INT REFERENCES plugins(id), target_cohort_id INT REFERENCES cohorts(id),
  min_plugin_version VARCHAR(50), max_plugin_version VARCHAR(50),
  starts_at TIMESTAMPTZ DEFAULT now(), expires_at TIMESTAMPTZ,
  survey_question TEXT, survey_choices JSONB, survey_allow_multiple BOOLEAN DEFAULT false,
  survey_allow_comment BOOLEAN DEFAULT false,
  status VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft','active','paused','completed','expired')),
  created_by VARCHAR(255), created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE IF NOT EXISTS survey_responses (
  id SERIAL PRIMARY KEY, communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
  client_uuid VARCHAR(255) NOT NULL, email VARCHAR(255), choices JSONB NOT NULL, comment TEXT,
  responded_at TIMESTAMPTZ DEFAULT now(), UNIQUE (communication_id, client_uuid));

CREATE TABLE IF NOT EXISTS communication_acks (
  communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
  client_uuid VARCHAR(255) NOT NULL, acked_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (communication_id, client_uuid));

-- KEYCLOAK
CREATE TABLE IF NOT EXISTS keycloak_clients (
  id SERIAL PRIMARY KEY, client_id VARCHAR(200) UNIQUE NOT NULL, realm VARCHAR(100) NOT NULL,
  description TEXT, client_type VARCHAR(20) DEFAULT 'public' CHECK (client_type IN ('public','confidential')),
  redirect_uris JSONB DEFAULT '[]'::jsonb, web_origins JSONB DEFAULT '["*"]'::jsonb,
  pkce_enabled BOOLEAN DEFAULT true, direct_access_grants BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE IF NOT EXISTS plugin_keycloak_clients (
  plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
  keycloak_client_id INT NOT NULL REFERENCES keycloak_clients(id) ON DELETE CASCADE,
  environment VARCHAR(20) NOT NULL CHECK (environment IN ('dev','int','prod')),
  PRIMARY KEY (plugin_id, keycloak_client_id, environment));

-- TELEMETRIE
CREATE TABLE IF NOT EXISTS device_telemetry_events (
  id BIGSERIAL PRIMARY KEY, client_uuid VARCHAR(255), email VARCHAR(255),
  span_name VARCHAR(100), span_ts TIMESTAMPTZ, attributes JSONB, plugin_version VARCHAR(50),
  source_ip INET, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_dte ON device_telemetry_events(client_uuid, span_ts DESC);

-- AUDIT
CREATE TABLE IF NOT EXISTS admin_audit_log (
  id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_email TEXT, actor_sub TEXT, action TEXT NOT NULL, resource_type TEXT,
  resource_id TEXT, payload JSONB, ip_address INET, user_agent TEXT);
CREATE INDEX IF NOT EXISTS idx_audit_at ON admin_audit_log(created_at DESC);

-- SEED
INSERT INTO plugins (slug, name, device_type, intent, category, maturity, access_mode, status) VALUES
  ('mirai-libreoffice','Assistant Mirai LibreOffice','libreoffice','Assistant IA pour LibreOffice','productivity','release','open','active'),
  ('mirai-matisse','Matisse Thunderbird','matisse','Extension IA pour Thunderbird','communication','beta','keycloak_group','active')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO plugin_aliases (alias, plugin_id) VALUES
  ('libreoffice', (SELECT id FROM plugins WHERE slug='mirai-libreoffice')),
  ('matisse', (SELECT id FROM plugins WHERE slug='mirai-matisse'))
ON CONFLICT DO NOTHING;

-- GRANTS
DO $$ BEGIN
  PERFORM 1 FROM pg_roles WHERE rolname='dev';
  IF FOUND THEN
    GRANT CONNECT ON DATABASE bootstrap TO dev;
    GRANT USAGE ON SCHEMA public TO dev;
    GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO dev;
    GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT,UPDATE ON SEQUENCES TO dev;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
```
</details>

---

## 3. Maturite et acces

| Maturite | Badge DSFR | Description |
|----------|------------|-------------|
| `dev` | gris `fr-badge--neutral` | Equipe dev uniquement |
| `alpha` | rouge `fr-badge--error` | Interne, experimental |
| `beta` | orange `fr-badge--warning` | Early adopters |
| `pre-release` | bleu `fr-badge--info` | Validation finale |
| `release` | vert `fr-badge--success` | Stable, tous |

| Mode d'acces | Verification |
|-------------|--------------|
| `open` | Aucune |
| `waitlist` | `plugin_waitlist.status = 'approved'` |
| `keycloak_group` | JWT `groups` contient `required_group` |

---

## 4. Dashboard admin (`/admin/`)

En haut du tableau de bord existant, ajouter :

**Encart disponibilite** (HTMX refresh 30s) : bandeau vert/orange/rouge avec detail par service + latence. Lien vers `/admin/debug`.

**Metriques adoption** (Chart.js) : courbe enrollments + filtres (type: Tous/LO/Matisse, periode: 1J/1S/1M/3M/6M) + 4 tuiles (total, nouveaux, % actifs, plugins).

Donnees depuis `/catalog/api/stats` (public, cache 5min) et `/admin/api/debug/health` (admin).

---

## 5. Onglets fiche plugin admin (`/admin/catalog/{id}`)

```
[Versions] [Changelog] [Environnements] [Keycloak] [Acces] [Alias] [Installations] [Editer]
```

| Onglet | Contenu |
|--------|---------|
| **Versions** | Liste, publier/deprecier/retirer, bouton "Deployer (1-2-3)" par version |
| **Changelog** | Markdown global du plugin |
| **Environnements** | Surcharges cle/valeur par env (dev/int/prod), dropdown cles connues, preview JSON |
| **Keycloak** | Clients par env, creer/associer, export JSON Keycloak, auto-gen `bootstrap-{slug}` |
| **Acces** | Selecteur maturite + mode + groupe KC. Waitlist avec validation/refus en masse |
| **Alias** | Tableau alias, appels 7j, devices uniques, tendance, estimation suppression (< 1%) |
| **Installations** | Liste client_uuid/email/version/derniere activite |
| **Editer** | Tous les champs + upload logo/mascotte (.png/.svg, max 2Mo, servi via `/catalog/icons/{slug}.png`) |

---

## 6. Page publique catalogue (`/catalog`)

DSFR via CDN. Sans auth. Style mirai.interieur.gouv.fr.

**Structure** : header gouv → encart disponibilite → hero + stats → metriques adoption (graphique + filtres) → grille plugins (cartes avec badges maturite, tags, version, installs, logo, lien code source) → prochainement → benefices → footer gouv.

**Fiche plugin** (`/catalog/{slug}`) : en-tete + stats → mode d'emploi (etapes par device_type) → telechargement → changelog → feedback utilisateurs → waitlist (si applicable).

---

## 7. API JSON publique

CORS ouvert, cache 5min, Swagger sur `/catalog/api/docs`.

| Route | Description |
|-------|-------------|
| `GET /catalog/api/plugins` | Liste plugins (slug, name, intent, maturity, version, installs, tags, urls) |
| `GET /catalog/api/plugins/{slug}` | Detail (+ description, changelog, download_extension, guide_url) |
| `GET /catalog/api/status` | Etat services (cache 30s) |
| `GET /catalog/api/stats?device_type=&period=1M` | Metriques adoption (timeseries + summary) |

Modeles Pydantic (`PluginSummary`, `PluginDetail`, `PluginListResponse`) avec exemples pour Swagger auto.

---

## 8. Assistance LLM

Toutes les features partagent `_llm_assist(system_prompt, user_content) → str`.

| Feature | Declencheur | Input → Output |
|---------|-------------|----------------|
| Analyse package | Upload dans "Nouveau plugin" | ZIP → nom, slug, intent, description, tags, categorie, changelog |
| Release notes | Bouton dans creation version | Package + version precedente → markdown nouveautes |
| Resume changelog | Auto a la publication | Release notes → 2-3 phrases |
| Suggestion tags | Bouton dans Editer | Description + changelog → tags courts |
| Redaction communication | Bouton dans formulaire comm | Type + plugin + version → titre + corps |
| Synthese feedbacks | Bouton dans resultats sondage | Commentaires → 3-5 themes |
| Triage waitlist | Bouton dans onglet Acces | Demandes → score pertinence + suggestion |

---

## 9. Page de debug (`/admin/debug`)

Verifie en parallele (max 12s) : PostgreSQL, S3, Keycloak OIDC/JWKS, LLM, telemetrie upstream, relay-assistant, queue worker.

4 panneaux : services (statut + latence), configuration (vars avec secrets masques), DB (lignes par table), systeme (version, uptime, hostname).

Refresh auto 30s. Badge rouge dans la nav si un service est down.

---

## 10. Endpoints monitoring (Grafana / Prometheus)

| Route | Format | Usage |
|-------|--------|-------|
| `GET /ops/health/full` | JSON | Sante detaillee, statut global ok/degraded/error |
| `GET /ops/metrics` | Prometheus text | Scrape : `dm_service_up`, `dm_service_latency_ms`, `dm_devices_enrolled_total`, `dm_queue_pending`, etc. |
| `GET /ops/metrics/adoption` | JSON | Timeseries adoption pour Grafana JSON datasource |

Statut global : `ok` (tout OK), `degraded` (non-critique down), `error` (critique down).

Alertes Grafana suggerees : DB down (critical), Keycloak down (high), latence > 2s (warning), dead letters > 10 (medium).

---

## 11. Nettoyage

| Action | Detail |
|--------|--------|
| Supprimer | `config/chrome/`, `config/edge/`, `config/firefox/`, `config/misc/` |
| Supprimer | `db/migrations/` (tout le dossier) |
| ConfigMap k8s | Retirer 4 entrees + 4 volume mounts |
| `main.py` | Supprimer `DEVICE_ALLOWLIST`, ajouter `_resolve_device()` |
| `router.py` | `DEVICE_TYPES` → 2 entrees |
| `schemas.py` | Pattern → `libreoffice\|matisse` |
| `config/libreoffice/*.json` | `config_path` → `/config/mirai-libreoffice/config.json` |
| `config/matisse/*.json` | `config_path` → `/config/mirai-matisse/config.json` |

---

## 12. Ordre d'implementation

Chaque etape est testable independamment. Valide chaque etape avant de passer a la suivante.

| # | Etape | Fichiers | Test |
|---|-------|----------|------|
| 1 | Remplacer `db/schema.sql`, supprimer `db/migrations/`, reset DB | `db/schema.sql` | `SELECT COUNT(*) FROM plugins` → 2 |
| 2 | Supprimer config/chrome,edge,firefox,misc + nettoyer k8s | `config/`, ConfigMap, Deployment | `ls config/` → libreoffice, matisse |
| 3 | `_resolve_device()` + injection device_name dans `main.py` | `app/main.py` | `GET /config/libreoffice/...` retourne `device_name: "mirai-libreoffice"` |
| 4 | `_apply_catalog_overrides()` dans `main.py` | `app/main.py` | INSERT override → GET config → valeur surchargee |
| 5 | Access control (keycloak_group + waitlist) | `app/main.py` | Plugin beta sans groupe → `access_denied: true` |
| 6 | Alias tracking + log | `app/main.py` | `GET /config/libreoffice/...` → row dans alias_access_log |
| 7 | CSS animations (spinner, pulse, progress) | `dm-admin.css` | Visuel sur `/admin/catalog/new` |
| 8 | Slug intelligent + tags interactifs | `catalog_plugin_new.html`, `router.py` | Auto-slug + check dispo |
| 9 | Upload logo plugin | `router.py`, `catalog_plugin.html` | Upload PNG → visible dans fiche |
| 10 | Onglet Environnements + preview JSON | `router.py`, `catalog_plugin.html` | Ajouter override → preview reflète |
| 11 | Onglet Keycloak + export JSON | `keycloak.py`, `router.py` | Creer client → telecharger JSON |
| 12 | Onglet Acces + waitlist | `router.py`, `catalog_plugin.html` | Demande waitlist → valider → acces OK |
| 13 | Onglet Alias + metriques migration | `router.py`, `catalog.py` | Stats alias affichees |
| 14 | Dashboard admin (dispo + adoption) | `router.py`, `dashboard.html` | Encart vert + graphique |
| 15 | Page debug `/admin/debug` | `router.py`, `debug.html` | Tous services verifies |
| 16 | Page publique `/catalog` (DSFR) | `main.py`, templates | Page accessible sans auth |
| 17 | Fiche publique `/catalog/{slug}` | `main.py`, templates | Mode d'emploi, changelog, feedback |
| 18 | Waitlist public + download | `main.py` | POST waitlist → row en DB |
| 19 | API JSON `/catalog/api/plugins` + Swagger | `main.py` | JSON retourne, `/catalog/api/docs` accessible |
| 20 | Endpoints monitoring `/ops/metrics` | `main.py` | Prometheus text valide |
| 21 | LLM assist (suggest, tags, notes, comm) | `router.py` | Upload plugin → champs pre-remplis |
| 22 | Mettre a jour config_path dans templates | `config/*.json` | `config_path` contient le slug |
| 23 | Build, deploy, tester E2E | `scripts/` | Rollout OK, `/healthz` 200 |
