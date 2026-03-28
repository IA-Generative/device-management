# Prompt — Catalogue v2

> Version : 2.1 — 2026-03-28
> Perimetre : device-management
> Plugins : **mirai-libreoffice** (alias: libreoffice), **mirai-matisse** (alias: matisse)

---

## Principe

Le `device_name` est l'identifiant unique partage entre le plugin et le serveur.
Le catalogue est une couche de gestion transparente : le plugin n'a pas besoin
de savoir qu'il existe.

```
device_name  = slug = identifiant universel    ex: "mirai-libreoffice"
device_type  = type interne (template config)  ex: "libreoffice"
alias        = retrocompatibilite              ex: "libreoffice" → "mirai-libreoffice"
```

---

## 1. Pipeline de configuration (coeur du systeme)

Quand un plugin appelle `/config/{x}/config.json?profile=dev` :

```
1. RESOLVE : x → (device_name, device_type, plugin_id, resolved_via)
   ├─ slug exact "mirai-libreoffice" → match direct
   ├─ alias "libreoffice" → lookup plugin_aliases → redirect + LOG
   └─ inconnu → 400

2. TEMPLATE : charge config/{device_type}/config.{profile}.json
   ex: config/libreoffice/config.dev.json

3. SUBSTITUTION : ${{VAR}} → valeurs env systeme

4. OVERRIDES DM : telemetrie, relay, etc. (existant, inchange)

5. OVERRIDES CATALOGUE : plugin_env_overrides WHERE plugin_id AND environment
   → surcharge keycloakClientId, llm_base_urls, etc. par env

6. KEYCLOAK CATALOGUE : plugin_keycloak_clients → injecte client_id + realm

7. ACCESS CONTROL :
   ├─ open → passe
   ├─ keycloak_group → verifie le groupe dans le JWT
   └─ waitlist → verifie plugin_waitlist.status = 'approved'
   Si refuse → retourne config minimale avec access_denied=true

8. INJECTION : force device_name + config_path dans la reponse
   (meme si appel via alias, la reponse contient le vrai slug)

9. SCRUB : secrets masques si pas de relay credentials

10. ENRICHMENT : campaigns, features, communications (existant)
```

### Implementation (`_resolve_device`)

```python
def _resolve_device(device: str, cur) -> tuple[str | None, str | None, int | None, str]:
    """Resolve slug/alias → (device_name, device_type, plugin_id, resolved_via)."""
    # 1. Slug exact
    cur.execute("""
        SELECT slug, device_type, id FROM plugins
        WHERE slug = %s AND status = 'active'
    """, (device,))
    row = cur.fetchone()
    if row:
        return row[0], row[1], row[2], "slug"

    # 2. Alias
    cur.execute("""
        SELECT p.slug, p.device_type, p.id
        FROM plugin_aliases a JOIN plugins p ON p.id = a.plugin_id
        WHERE a.alias = %s AND p.status = 'active'
    """, (device,))
    row = cur.fetchone()
    if row:
        return row[0], row[1], row[2], "alias"

    return None, None, None, "unknown"
```

### Implementation (`_apply_catalog_overrides`)

```python
def _apply_catalog_overrides(cfg: dict, *, plugin_id: int, profile: str, cur) -> dict:
    """Applique les overrides catalogue + Keycloak pour un plugin et profil."""
    config_obj = cfg.get("config")
    if not isinstance(config_obj, dict):
        return cfg

    # Overrides env
    cur.execute("""
        SELECT key, value FROM plugin_env_overrides
        WHERE plugin_id = %s AND environment = %s
    """, (plugin_id, profile))
    for key, value in cur.fetchall():
        config_obj[key] = value

    # Client Keycloak specifique
    cur.execute("""
        SELECT kc.client_id, kc.realm
        FROM plugin_keycloak_clients pkc
        JOIN keycloak_clients kc ON kc.id = pkc.keycloak_client_id
        WHERE pkc.plugin_id = %s AND pkc.environment = %s LIMIT 1
    """, (plugin_id, profile))
    kc = cur.fetchone()
    if kc:
        config_obj["keycloakClientId"] = kc[0]
        config_obj["keycloakRealm"] = kc[1]

    return cfg
```

### Injection device_name (etape 8)

```python
config_obj = cfg.get("config")
if isinstance(config_obj, dict) and device_name:
    config_obj["device_name"] = device_name
    config_obj["config_path"] = f"/config/{device_name}/config.json"
```

### Migration douce des alias

Un plugin appelant `/config/libreoffice/config.json` recoit :
```json
{ "config": { "device_name": "mirai-libreoffice", "config_path": "/config/mirai-libreoffice/config.json" } }
```
Au prochain cycle, il utilise le nouveau chemin. Zero action manuelle.

---

## 2. Modele de donnees

### Migration 007 (unique)

```sql
-- ─── ALTER plugins ──────────────────────────────────────────────

ALTER TABLE plugins ADD COLUMN IF NOT EXISTS maturity VARCHAR(20) DEFAULT 'release'
    CHECK (maturity IN ('dev', 'alpha', 'beta', 'pre-release', 'release'));
ALTER TABLE plugins ADD COLUMN IF NOT EXISTS access_mode VARCHAR(20) DEFAULT 'open'
    CHECK (access_mode IN ('open', 'waitlist', 'keycloak_group'));
ALTER TABLE plugins ADD COLUMN IF NOT EXISTS required_group VARCHAR(200);
ALTER TABLE plugins ADD COLUMN IF NOT EXISTS source_url TEXT;  -- lien vers le code source (GitHub, GitLab, etc.)

-- ─── Aliases ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plugin_aliases (
    alias VARCHAR(100) PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE
);

-- ─── Alias access tracking ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS alias_access_log (
    id BIGSERIAL PRIMARY KEY,
    alias VARCHAR(100) NOT NULL,
    slug VARCHAR(100) NOT NULL,
    plugin_id INT NOT NULL,
    client_uuid VARCHAR(255),
    source_ip INET,
    accessed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_aal_alias ON alias_access_log(alias, accessed_at DESC);

-- ─── Env overrides ��────────────────────────────────────────���────

CREATE TABLE IF NOT EXISTS plugin_env_overrides (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    environment VARCHAR(20) NOT NULL CHECK (environment IN ('dev', 'int', 'prod')),
    key VARCHAR(200) NOT NULL,
    value TEXT NOT NULL,
    is_secret BOOLEAN DEFAULT false,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (plugin_id, environment, key)
);

-- ─── Keycloak clients ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS keycloak_clients (
    id SERIAL PRIMARY KEY,
    client_id VARCHAR(200) UNIQUE NOT NULL,
    realm VARCHAR(100) NOT NULL,
    description TEXT,
    client_type VARCHAR(20) DEFAULT 'public' CHECK (client_type IN ('public', 'confidential')),
    redirect_uris JSONB DEFAULT '[]'::jsonb,
    web_origins JSONB DEFAULT '["*"]'::jsonb,
    pkce_enabled BOOLEAN DEFAULT true,
    direct_access_grants BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plugin_keycloak_clients (
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    keycloak_client_id INT NOT NULL REFERENCES keycloak_clients(id) ON DELETE CASCADE,
    environment VARCHAR(20) NOT NULL CHECK (environment IN ('dev', 'int', 'prod')),
    PRIMARY KEY (plugin_id, keycloak_client_id, environment)
);

-- ─── Waitlist ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plugin_waitlist (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    client_uuid VARCHAR(255),
    reason TEXT,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (plugin_id, email)
);

-- ─── Seed ───────────────────────────────────────────────────────

INSERT INTO plugins (slug, name, device_type, intent, category, maturity, access_mode, status)
VALUES
  ('mirai-libreoffice', 'Assistant Mirai LibreOffice', 'libreoffice',
   'Assistant IA integre a LibreOffice pour la redaction', 'productivity', 'release', 'open', 'active'),
  ('mirai-matisse', 'Matisse Thunderbird', 'matisse',
   'Extension IA pour Thunderbird', 'communication', 'beta', 'keycloak_group', 'active')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO plugin_aliases (alias, plugin_id) VALUES
  ('libreoffice', (SELECT id FROM plugins WHERE slug = 'mirai-libreoffice')),
  ('matisse', (SELECT id FROM plugins WHERE slug = 'mirai-matisse'))
ON CONFLICT DO NOTHING;

-- Grants
DO $$ BEGIN
  PERFORM 1 FROM pg_roles WHERE rolname = 'dev';
  IF FOUND THEN
    GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO dev;
    GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;
```

---

## 3. Maturite et controle d'acces

| Maturite | Badge DSFR | Description |
|----------|------------|-------------|
| `dev` | `fr-badge--neutral` gris | Developpement en cours, equipe dev uniquement |
| `alpha` | `fr-badge--error` rouge | Interne, experimental |
| `beta` | `fr-badge--warning` orange | Early adopters valides |
| `pre-release` | `fr-badge--info` bleu | Validation finale |
| `release` | `fr-badge--success` vert | Stable, tous |

| Mode d'acces | Telechargement | Verification |
|-------------|----------------|--------------|
| `open` | Libre | Aucune |
| `waitlist` | Apres validation admin | `plugin_waitlist.status = 'approved'` |
| `keycloak_group` | Membres du groupe KC | JWT `groups` contient `required_group` |

---

## 4. Interface admin (onglets fiche plugin)

```
[Versions] [Changelog] [Environnements] [Keycloak] [Acces] [Alias] [Installations] [Editer]
```

### Logo / Mascotte du plugin

Chaque plugin peut avoir un logo (mascotte, icone produit) visible dans le catalogue
et la fiche publique. Le logo remplace les initiales generiques (LO, TB).

**Upload** : dans le formulaire "Nouveau plugin" et dans l'onglet "Editer" de la fiche :
- Champ file input : `accept=".png,.jpg,.jpeg,.svg,.webp"`
- Taille max : 2 Mo
- Dimensions recommandees : 256x256 px (carre), redimensionne automatiquement si besoin
- Stocke localement dans `/data/content/icons/{slug}.{ext}` (ou S3 si configure)
- Le champ `icon_path` en base pointe vers le fichier

**Serving** : route publique sans auth pour afficher le logo dans le catalogue :
```
GET /catalog/icons/{slug}.png  → sert le fichier depuis icon_path
```

**Affichage** : dans les cartes catalogue et la fiche plugin :
```html
{% if plugin.icon_path %}
  <img src="/catalog/icons/{{ plugin.slug }}.png"
       alt="{{ plugin.name }}"
       style="width:64px;height:64px;border-radius:12px;object-fit:cover;">
{% else %}
  <div class="dm-plugin-icon">{{ plugin.device_type[:2]|upper }}</div>
{% endif %}
```

Si pas de logo, fallback sur les initiales du device_type (comportement actuel).

### Environnements

Surcharges cle/valeur par profil (dev/int/prod). Dropdown des cles connues extrait du template config.
Bouton "Previsualiser JSON" pour voir la config finale telle que le plugin la recevrait.

### Keycloak

Tableau clients par environnement. Boutons : associer existant, creer nouveau, telecharger JSON d'import.
Client ID auto-genere : `bootstrap-{slug}` (+ `-dev`/`-int` pour les env non-prod).
Quand un client est associe, les overrides `keycloakClientId` et `keycloakRealm` sont auto-crees.

### Acces

Selecteurs maturite + mode d'acces + groupe KC.
Tableau liste d'attente avec validation/refus individuel ou en masse.

### Alias (avec metriques de migration)

Tableau : alias, appels 7j, devices uniques, tendance.
Ratio slug/alias avec estimation du delai avant suppression possible (seuil < 1%).
Purge auto des logs > 90 jours.

---

## 5. Page publique catalogue (style mirai.interieur.gouv.fr)

### Design

DSFR via CDN (`@gouvfr/dsfr@1.12.1`). Header gouvernemental, footer avec liens officiels.

- **Hero** : titre + 3 `fr-callout` stats (agents equipes, plugins actifs, MAJ automatiques)
- **Grille** : `fr-card fr-card--horizontal` avec tags `fr-tag`, badge maturite, version, installs
- **Prochainement** : plugins en `draft` avec `fr-badge--info`
- **Benefices** : 3x `fr-tile` (outils adaptes, gain de temps, securite souveraine)

### Fiche plugin (`/catalog/{slug}`)

1. **En-tete** : icone, nom, version, badges maturite + acces, intent, tags features
2. **Code source** : lien discret avec icone GitHub (si `homepage_url` defini), affiche en petit sous le nom
3. **Statistiques** : 4 tuiles (installs, version, adoption, taille)
3. **Mode d'emploi** : etapes numerotees specifiques au device_type + bouton telechargement
4. **Nouveautes** : release notes de la derniere version
5. **Changelog complet** : toutes les versions (expandable)
6. **Feedback utilisateurs** : derniers commentaires positifs (depuis les sondages)
7. **Liste d'attente** (si `access_mode = 'waitlist'`) : formulaire email + raison

### Routes publiques

| Route | Auth | Description |
|-------|------|-------------|
| `GET /catalog` | Non | Page d'accueil catalogue |
| `GET /catalog/{slug}` | Non | Fiche plugin |
| `GET /catalog/{slug}/download` | Selon access_mode | Telechargement derniere version |
| `POST /catalog/{slug}/waitlist` | Non | Inscription liste d'attente |

---

## 6. API JSON publique (integration mirai.interieur.gouv.fr)

CORS ouvert, cache 5 min, zero auth.

### Documentation Swagger/OpenAPI

Exposer une page de documentation interactive a `/catalog/api/docs` :

- Utiliser le **Swagger UI integre de FastAPI** (`/catalog/api/docs` pour Swagger, `/catalog/api/redoc` pour ReDoc)
- Creer un sous-router FastAPI dedie avec `tags` et `description` pour une doc claire
- Chaque endpoint documente avec `summary`, `description`, `response_model` (Pydantic)

```python
from fastapi import APIRouter
from pydantic import BaseModel, Field

catalog_api = APIRouter(prefix="/catalog/api", tags=["Catalogue public"])

class PluginSummary(BaseModel):
    slug: str = Field(..., example="mirai-libreoffice")
    name: str = Field(..., example="Assistant Mirai LibreOffice")
    intent: str = Field("", example="Augmentez votre productivite...")
    device_type: str = Field(..., example="libreoffice")
    maturity: str = Field("release", example="beta")
    maturity_label: str = Field("Stable", example="Beta")
    access_mode: str = Field("open", example="open")
    latest_version: str | None = Field(None, example="2.1.0")
    install_count: int = Field(0, example=1245)
    key_features: list[str] = Field([], example=["Redaction IA", "Resume"])
    icon_url: str | None = Field(None, example="https://bootstrap.fake-domain.name/catalog/icons/mirai-libreoffice.png")
    detail_url: str = Field(..., example="https://bootstrap.fake-domain.name/catalog/mirai-libreoffice")
    download_url: str = Field(..., example="https://bootstrap.fake-domain.name/catalog/mirai-libreoffice/download")

class PluginDetail(PluginSummary):
    description: str = Field("", example="Description detaillee du plugin...")
    category: str = Field("productivity")
    publisher: str = Field("DNUM")
    homepage_url: str | None = None
    source_url: str | None = Field(None, example="https://github.com/IA-Generative/AssistantMiraiLibreOffice")
    support_email: str | None = None
    changelog_summary: str = Field("", example="v2.1.0 : mode hors-ligne, fix telemetrie")
    download_extension: str = Field("", example=".oxt")
    download_size_bytes: int | None = None
    install_guide_url: str | None = None

class PluginListResponse(BaseModel):
    plugins: list[PluginSummary]
    total: int
    generated_at: str

@catalog_api.get("/plugins", response_model=PluginListResponse,
                 summary="Liste des plugins",
                 description="Retourne tous les plugins actifs et publics du catalogue.")
def api_list_plugins(): ...

@catalog_api.get("/plugins/{slug}", response_model=PluginDetail,
                 summary="Detail d'un plugin",
                 description="Retourne le detail complet d'un plugin par son slug (device_name).")
def api_get_plugin(slug: str): ...
```

Monter le sous-router dans main.py :
```python
from app.catalog_api import catalog_api
app.include_router(catalog_api)
```

Pages accessibles :
- `/catalog/api/docs` — Swagger UI interactif (test des endpoints en live)
- `/catalog/api/redoc` — Documentation ReDoc (lecture)
- `/catalog/api/openapi.json` — Schema OpenAPI 3.x brut (pour generation de clients)

### `GET /catalog/api/plugins`

```json
{
  "plugins": [
    {
      "slug": "mirai-libreoffice",
      "name": "Assistant Mirai LibreOffice",
      "intent": "Augmentez votre productivite...",
      "device_type": "libreoffice",
      "maturity": "release", "maturity_label": "Stable",
      "access_mode": "open",
      "latest_version": "2.1.0",
      "install_count": 1245,
      "key_features": ["Redaction IA", "Reformulation", "Resume"],
      "detail_url": "https://bootstrap.fake-domain.name/catalog/mirai-libreoffice",
      "download_url": "https://bootstrap.fake-domain.name/catalog/mirai-libreoffice/download"
    }
  ],
  "total": 2,
  "generated_at": "2026-03-28T14:30:00Z"
}
```

### `GET /catalog/api/plugins/{slug}`

Idem + `description`, `changelog_summary`, `download_extension`, `download_size_bytes`,
`homepage_url`, `support_email`, `install_guide_url`.

### Integration cote mirai

```html
<div id="dm-plugins" class="fr-grid-row fr-grid-row--gutters"></div>
<script>
fetch('https://bootstrap.fake-domain.name/catalog/api/plugins')
  .then(r => r.json())
  .then(data => {
    const c = document.getElementById('dm-plugins');
    for (const p of data.plugins) {
      const badge = {alpha:'error',beta:'warning','pre-release':'info',release:'success'}[p.maturity];
      const tags = p.key_features.map(f => `<li><p class="fr-tag">${f}</p></li>`).join('');
      c.innerHTML += `
        <div class="fr-col-12 fr-col-md-6">
          <div class="fr-card fr-enlarge-link">
            <div class="fr-card__body"><div class="fr-card__content">
              <h3 class="fr-card__title"><a href="${p.detail_url}">${p.name}</a></h3>
              <p class="fr-card__desc">${p.intent}</p>
              <div class="fr-card__start">
                <p class="fr-badge fr-badge--${badge} fr-badge--no-icon">${p.maturity_label}</p>
                <span style="margin-left:.5rem;font-size:.85rem;color:#666">
                  v${p.latest_version||'—'} — ${p.install_count} installs
                </span>
              </div>
              <div class="fr-card__end"><ul class="fr-tags-group">${tags}</ul></div>
            </div></div>
          </div>
        </div>`;
    }
  });
</script>
```

---

## 7. Nettoyage (supprimer chrome/edge/firefox/misc)

| Action | Fichiers |
|--------|----------|
| Supprimer | `config/chrome/`, `config/edge/`, `config/firefox/`, `config/misc/` (12 fichiers) |
| ConfigMap k8s | Supprimer 4 entrees + 4 volume mounts |
| `DEVICE_ALLOWLIST` | Supprimer (remplace par `_resolve_device()`) |
| `DEVICE_TYPES` | 5 → 2 entrees (libreoffice, matisse) |
| `schemas.py` | Pattern → `libreoffice\|matisse` |
| Templates | Retirer references chrome/edge/firefox dans JS |
| `config/libreoffice/*.json` | `config_path` → `/config/mirai-libreoffice/config.json` |
| `config/matisse/*.json` | `config_path` → `/config/mirai-matisse/config.json` |

Templates config sur disque : **inchanges** (organises par device_type).
Manifests k8s : inchanges sauf suppression des 4 device types retires.

---

## 8. Fresh start DB + fichiers a creer/modifier

### Consolidation du schema

Le schema actuel est le resultat de 6 migrations incrementales (schema.sql + 002 a 006).
Comme la base n'est pas en production reelle, on consolide **tout** en un seul `schema.sql`
propre et on supprime les migrations individuelles.

**Strategie** :
1. Ecrire un nouveau `db/schema.sql` complet et optimise (toutes les tables)
2. Supprimer `db/migrations/` (plus de migrations incrementales)
3. Sur Scaleway : `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` puis re-appliquer
4. Les donnees operationnelles existantes (47k device_connections, 37k provisioning, etc.)
   seront recreees naturellement par les plugins qui se reconnecteront

### Nouveau `db/schema.sql` (remplace schema.sql + toutes les migrations)

```sql
-- Device Management — Schema complet (v2, fresh start)
-- Usage: psql -v ON_ERROR_STOP=1 -d bootstrap -f db/schema.sql

-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- Dev role (local)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dev') THEN
    CREATE ROLE dev LOGIN PASSWORD 'dev';
  END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

-- Trigger function
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

-- ═══════════════════════════════════════════════════════════════
-- CORE : enrollment, connections, relay, queue
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS provisioning (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email CITEXT NOT NULL,
    device_name TEXT NOT NULL,
    client_uuid UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','ENROLLED','REVOKED','FAILED')),
    encryption_key TEXT NOT NULL,
    comments TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_active
    ON provisioning (client_uuid) WHERE status IN ('PENDING','ENROLLED');
CREATE INDEX IF NOT EXISTS idx_provisioning_email ON provisioning(email);
CREATE INDEX IF NOT EXISTS idx_provisioning_client ON provisioning(client_uuid);

CREATE TABLE IF NOT EXISTS device_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email CITEXT NOT NULL,
    client_uuid UUID NOT NULL,
    action TEXT NOT NULL DEFAULT 'UNKNOWN',
    encryption_key_fingerprint TEXT NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_ip INET,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_client_at ON device_connections(client_uuid, connected_at DESC);
CREATE INDEX IF NOT EXISTS idx_dc_email_at ON device_connections(email, connected_at DESC);

CREATE TABLE IF NOT EXISTS relay_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_uuid UUID NOT NULL,
    email CITEXT NOT NULL,
    relay_client_id TEXT NOT NULL,
    relay_key_hash TEXT NOT NULL,
    allowed_targets TEXT[] NOT NULL DEFAULT ARRAY['keycloak']::text[],
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    comments TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_relay_active_client
    ON relay_clients(client_uuid) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_relay_active_id
    ON relay_clients(relay_client_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS queue_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','dead')),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 8,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    lock_owner TEXT,
    dedupe_key TEXT,
    completed_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_qj_poll ON queue_jobs(status, next_attempt_at, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qj_dedupe ON queue_jobs(topic, dedupe_key);

CREATE TABLE IF NOT EXISTS queue_job_dead_letters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    job_id UUID NOT NULL,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    dedupe_key TEXT,
    attempts INT NOT NULL,
    max_attempts INT NOT NULL,
    last_error TEXT
);

-- ═══════════════════════════════════════════════════════════════
-- CATALOGUE : plugins, versions, aliases, env overrides
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS plugins (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    intent TEXT,
    key_features JSONB DEFAULT '[]'::jsonb,
    changelog TEXT,
    icon_url TEXT,
    device_type VARCHAR(50) NOT NULL,
    category VARCHAR(100) DEFAULT 'productivity',
    homepage_url TEXT,
    source_url TEXT,
    icon_path TEXT,                       -- chemin local du logo (ex: /data/content/icons/mirai-libreoffice.png)
    support_email TEXT,
    publisher VARCHAR(200) DEFAULT 'DNUM',
    visibility VARCHAR(20) DEFAULT 'public'
        CHECK (visibility IN ('public','internal','hidden')),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('draft','active','deprecated','removed')),
    maturity VARCHAR(20) DEFAULT 'release'
        CHECK (maturity IN ('dev','alpha','beta','pre-release','release')),
    access_mode VARCHAR(20) DEFAULT 'open'
        CHECK (access_mode IN ('open','waitlist','keycloak_group')),
    required_group VARCHAR(200),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plugin_aliases (
    alias VARCHAR(100) PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS plugin_env_overrides (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    environment VARCHAR(20) NOT NULL CHECK (environment IN ('dev','int','prod')),
    key VARCHAR(200) NOT NULL,
    value TEXT NOT NULL,
    is_secret BOOLEAN DEFAULT false,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (plugin_id, environment, key)
);

CREATE TABLE IF NOT EXISTS plugin_waitlist (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    client_uuid VARCHAR(255),
    reason TEXT,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (plugin_id, email)
);

CREATE TABLE IF NOT EXISTS alias_access_log (
    id BIGSERIAL PRIMARY KEY,
    alias VARCHAR(100) NOT NULL,
    slug VARCHAR(100) NOT NULL,
    plugin_id INT NOT NULL,
    client_uuid VARCHAR(255),
    source_ip INET,
    accessed_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aal_alias ON alias_access_log(alias, accessed_at DESC);

-- ═══════════════════════════════════════════════════════════════
-- ARTIFACTS, VERSIONS, DEPLOYEMENT
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS artifacts (
    id SERIAL PRIMARY KEY,
    device_type VARCHAR(50) NOT NULL,
    platform_variant VARCHAR(50),
    version VARCHAR(50) NOT NULL,
    s3_path TEXT,
    checksum VARCHAR(128),
    min_host_version VARCHAR(50),
    max_host_version VARCHAR(50),
    changelog_url TEXT,
    is_active BOOLEAN DEFAULT true,
    released_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (device_type, platform_variant, version)
);

CREATE TABLE IF NOT EXISTS plugin_versions (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    artifact_id INT REFERENCES artifacts(id),
    version VARCHAR(50) NOT NULL,
    release_notes TEXT,
    download_url TEXT,
    min_host_version VARCHAR(50),
    max_host_version VARCHAR(50),
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft','published','deprecated','yanked')),
    distribution_mode VARCHAR(20) DEFAULT 'managed'
        CHECK (distribution_mode IN ('managed','download_link','store','manual')),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (plugin_id, version)
);

CREATE TABLE IF NOT EXISTS plugin_installations (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id),
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    installed_version VARCHAR(50),
    installed_at TIMESTAMPTZ DEFAULT now(),
    last_seen_at TIMESTAMPTZ DEFAULT now(),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active','inactive','uninstalled')),
    UNIQUE (plugin_id, client_uuid)
);

-- ═══════════════════════════════════════════════════════════════
-- CAMPAGNES, COHORTES, FEATURE FLAGS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS cohorts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    type VARCHAR(20) NOT NULL CHECK (type IN ('manual','percentage','email_pattern','keycloak_group')),
    config JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cohort_members (
    cohort_id INT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    identifier_type VARCHAR(20) NOT NULL CHECK (identifier_type IN ('email','client_uuid')),
    identifier_value VARCHAR(255) NOT NULL,
    added_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (cohort_id, identifier_type, identifier_value)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    type VARCHAR(30) DEFAULT 'plugin_update'
        CHECK (type IN ('plugin_update','config_patch','feature_set')),
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft','active','paused','completed','rolled_back')),
    target_cohort_id INT REFERENCES cohorts(id),
    artifact_id INT REFERENCES artifacts(id),
    rollback_artifact_id INT REFERENCES artifacts(id),
    rollout_config JSONB,
    urgency VARCHAR(10) DEFAULT 'normal' CHECK (urgency IN ('low','normal','critical')),
    deadline_at TIMESTAMPTZ,
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaign_device_status (
    campaign_id INT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending','notified','updated','failed','rolled_back')),
    version_before VARCHAR(50),
    version_after VARCHAR(50),
    error_message TEXT,
    last_contact_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (campaign_id, client_uuid)
);
CREATE INDEX IF NOT EXISTS idx_cds_campaign ON campaign_device_status(campaign_id, status);

CREATE TABLE IF NOT EXISTS feature_flags (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    default_value BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feature_flag_overrides (
    feature_id INT NOT NULL REFERENCES feature_flags(id) ON DELETE CASCADE,
    cohort_id INT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    value BOOLEAN NOT NULL,
    min_plugin_version VARCHAR(50),
    PRIMARY KEY (feature_id, cohort_id)
);

-- ═══════════════════════════════════════════════════════════════
-- COMMUNICATIONS, SONDAGES
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS communications (
    id SERIAL PRIMARY KEY,
    type VARCHAR(20) NOT NULL CHECK (type IN ('announcement','alert','survey','changelog')),
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,
    priority VARCHAR(10) DEFAULT 'normal' CHECK (priority IN ('low','normal','high','critical')),
    target_plugin_id INT REFERENCES plugins(id),
    target_cohort_id INT REFERENCES cohorts(id),
    min_plugin_version VARCHAR(50),
    max_plugin_version VARCHAR(50),
    starts_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ,
    survey_question TEXT,
    survey_choices JSONB,
    survey_allow_multiple BOOLEAN DEFAULT false,
    survey_allow_comment BOOLEAN DEFAULT false,
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft','active','paused','completed','expired')),
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS survey_responses (
    id SERIAL PRIMARY KEY,
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    choices JSONB NOT NULL,
    comment TEXT,
    responded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (communication_id, client_uuid)
);

CREATE TABLE IF NOT EXISTS communication_acks (
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    acked_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (communication_id, client_uuid)
);

-- ═══════════════════════════════════════════════════════════════
-- KEYCLOAK CLIENTS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS keycloak_clients (
    id SERIAL PRIMARY KEY,
    client_id VARCHAR(200) UNIQUE NOT NULL,
    realm VARCHAR(100) NOT NULL,
    description TEXT,
    client_type VARCHAR(20) DEFAULT 'public' CHECK (client_type IN ('public','confidential')),
    redirect_uris JSONB DEFAULT '[]'::jsonb,
    web_origins JSONB DEFAULT '["*"]'::jsonb,
    pkce_enabled BOOLEAN DEFAULT true,
    direct_access_grants BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plugin_keycloak_clients (
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    keycloak_client_id INT NOT NULL REFERENCES keycloak_clients(id) ON DELETE CASCADE,
    environment VARCHAR(20) NOT NULL CHECK (environment IN ('dev','int','prod')),
    PRIMARY KEY (plugin_id, keycloak_client_id, environment)
);

-- ═══════════════════════════════════════════════════════════════
-- TELEMETRIE (events extraits)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS device_telemetry_events (
    id BIGSERIAL PRIMARY KEY,
    client_uuid VARCHAR(255),
    email VARCHAR(255),
    span_name VARCHAR(100),
    span_ts TIMESTAMPTZ,
    attributes JSONB,
    plugin_version VARCHAR(50),
    source_ip INET,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dte_client ON device_telemetry_events(client_uuid, span_ts DESC);

-- ═══════════════════════════════════════════════════════════════
-- AUDIT
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_email TEXT,
    actor_sub TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    payload JSONB,
    ip_address INET,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON admin_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON admin_audit_log(actor_email);

-- ═══════════════════════════════════════════════════════════════
-- TRIGGERS updated_at
-- ═══════════════════════════════════════════════════════════════

DO $$ BEGIN
  FOR t IN SELECT unnest(ARRAY[
    'provisioning','relay_clients','queue_jobs','plugins',
    'plugin_env_overrides','cohorts','campaigns','feature_flags',
    'communications','keycloak_clients'
  ]) AS name LOOP
    EXECUTE format(
      'CREATE TRIGGER IF NOT EXISTS trg_%s_updated_at
       BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
      t.name, t.name
    );
  END LOOP;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

-- ═══════════════════════════════════════════════════════════════
-- SEED DATA
-- ═══════════════════════════════════════════════════════════════

INSERT INTO plugins (slug, name, device_type, intent, category, maturity, access_mode, status)
VALUES
  ('mirai-libreoffice', 'Assistant Mirai LibreOffice', 'libreoffice',
   'Assistant IA integre a LibreOffice pour la redaction', 'productivity', 'release', 'open', 'active'),
  ('mirai-matisse', 'Matisse Thunderbird', 'matisse',
   'Extension IA pour Thunderbird', 'communication', 'beta', 'keycloak_group', 'active')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO plugin_aliases (alias, plugin_id) VALUES
  ('libreoffice', (SELECT id FROM plugins WHERE slug = 'mirai-libreoffice')),
  ('matisse', (SELECT id FROM plugins WHERE slug = 'mirai-matisse'))
ON CONFLICT DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- GRANTS (dev role)
-- ═══════════════════════════════════════════════════════════════

DO $$ BEGIN
  PERFORM 1 FROM pg_roles WHERE rolname = 'dev';
  IF FOUND THEN
    GRANT CONNECT ON DATABASE bootstrap TO dev;
    GRANT USAGE ON SCHEMA public TO dev;
    GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO dev;
    GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE,SELECT,UPDATE ON SEQUENCES TO dev;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;
```

### Script de reset

```bash
# Reset complet de la base (Scaleway)
kubectl -n bootstrap exec deploy/device-management -- python -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/bootstrap')
conn.autocommit = True
cur = conn.cursor()
cur.execute('DROP SCHEMA public CASCADE')
cur.execute('CREATE SCHEMA public')
with open('/app/db/schema.sql') as f:
    cur.execute(f.read())
cur.execute(\"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\")
print([r[0] for r in cur.fetchall()])
conn.close()
"
```

### Optimisations par rapport a l'ancien schema

| Optimisation | Detail |
|---|---|
| **Pas d'enum Postgres** | `CHECK` constraints au lieu de `CREATE TYPE` (plus simple a modifier) |
| **Pas de `disconnected_at`** | Supprime de device_connections (jamais utilise) |
| **Tables bundles retirees** | `bundles` et `bundle_plugins` supprimees (pas utilise, a re-ajouter si besoin) |
| **Indexes minimaux** | Seulement les indexes reellement utilises par les queries |
| **Triggers consolides** | Un seul bloc DO pour tous les triggers `updated_at` |
| **Seed data inclus** | Les 2 plugins + alias sont crees avec le schema |
| **Grants robustes** | `ALTER DEFAULT PRIVILEGES` pour les futures tables |
| `app/main.py` | `_resolve_device()`, `_apply_catalog_overrides()`, injection device_name, access control, supprimer DEVICE_ALLOWLIST |
| `app/admin/router.py` | DEVICE_TYPES→2, routes env/keycloak/alias/acces/waitlist |
| `app/admin/services/keycloak.py` | CRUD clients, export JSON, link/unlink |
| `app/admin/services/catalog.py` | CRUD overrides, alias stats, preview config, waitlist |
| `app/admin/templates/catalog_plugin.html` | 6 onglets (versions, changelog, env, keycloak, acces, alias) |
| `app/admin/templates/catalog_plugin_new.html` | Animation spinner, slug intelligent, upload logo |
| `app/admin/static/dm-admin.css` | Spinner, pulse, indeterminate progress |
| `app/catalog_public/` | 3 templates DSFR (base, accueil, fiche plugin) |
| `config/libreoffice/*.json` | config_path → mirai-libreoffice |
| `config/matisse/*.json` | config_path → mirai-matisse |
| k8s ConfigMap + deployment | Supprimer chrome/edge/firefox/misc |

### Ordre d'implementation

1. Migration 007 + seed data
2. Supprimer config/chrome,edge,firefox,misc + nettoyer k8s
3. `_resolve_device()` + `_apply_catalog_overrides()` + injection device_name dans main.py
4. Access control (keycloak_group + waitlist) dans main.py
5. Alias tracking + log + purge
6. CSS animations (spinner, pulse, progress)
7. Slug intelligent dans le formulaire catalogue
8. Onglet Environnements (overrides + preview JSON)
9. Onglet Keycloak (clients + export JSON)
10. Onglet Acces (maturite + waitlist)
11. Onglet Alias (metriques migration)
12. Page publique catalogue DSFR (/catalog)
13. Fiche publique plugin (/catalog/{slug}) avec mode d'emploi, changelog, feedback
14. Formulaire waitlist public
15. API JSON publique (/catalog/api/plugins)
16. Mettre a jour config_path dans les templates config
17. Deployer et tester
