# Prompt — Onboarding de plugins : decouplage cluster / catalogue

> Version : 1.1 — 2026-03-28
> Perimetre : device-management
> Environnements : **local**, **dev**, **int**, **prod**
> Objectif : separer le deploiement du cluster de l'enregistrement des plugins

---

## Probleme actuel

Aujourd'hui, deployer un plugin necessite de toucher 5 endroits :

1. `config/{device_type}/config.{profile}.json` — templates sur disque
2. `deploy/k8s/base/manifests/10-configmap-device-management.yaml` — ConfigMap k8s
3. `deploy/k8s/base/manifests/20-device-management-deployment.yaml` — volume mounts
4. `db/schema.sql` — seed data (INSERT INTO plugins)
5. Rebuild + redeploy l'image Docker

C'est trop couplé. Ajouter un plugin = changer le code du serveur.

## Vision

```
Temps 1 : Deployer le cluster DM (une seule fois, generique)
           → Aucune connaissance des plugins specifiques
           → Le DM sait servir des configs mais n'en contient aucune en dur

Temps 2 : Enregistrer un plugin via l'admin UI ou l'API
           → Upload du package .oxt/.xpi → analyse IA → fiche catalogue creee
           → Le plugin fournit son propre template config (bundle dans le package)
           → L'admin ajuste les overrides par environnement
           → Aucun redeploy du cluster necessaire
```

## Solution : le template config vient du plugin

### Concept

Le template de configuration est fourni de **deux manieres** (au choix) :

**Option A — Bundle dans le package** : `dm-config.json` est inclus dans le ZIP du plugin.
Le DM l'extrait automatiquement a l'enregistrement, le stocke en base, puis
**retire le fichier du binaire** distribue aux utilisateurs (les cles serveur
n'ont pas a etre dans l'artifact installe).

```
mirai-2.1.0.oxt (upload par l'admin)
  ├── manifest.xml
  ├── src/
  ├── readme.md
  └── dm-config.json          ← extrait puis retire du binaire distribue
```

**Option B — Upload separe** : l'admin uploade le binaire ET le `dm-config.json`
separement dans le formulaire. Utile quand le developpeur du plugin ne veut pas
modifier son package, ou quand l'admin veut fournir un template personnalise.

```
Formulaire admin :
  [1. Fichier plugin (.oxt)]     ← binaire tel quel
  [2. dm-config.json (optionnel)] ← template config separe
```

### 4 environnements

Le DM gere 4 profils d'environnement. Le template config est commun,
les overrides sont specifiques par profil :

| Profil | Usage | Exemple base URL |
|--------|-------|------------------|
| `local` | Dev poste developpeur | `http://localhost:3001` |
| `dev` | Environnement de dev partage | `http://localhost:3001` ou serveur dev |
| `int` | Integration / recette | `https://bootstrap-int.domain.name` |
| `prod` | Production | `https://bootstrap.domain.name` |

```
                    Template config (commun)
                    ┌─────────────────────┐
                    │  dm-config.json      │
                    │  (valeurs par defaut)│
                    └──────────┬──────────┘
                               │
          ┌────────┬───────────┼───────────┬──────────┐
          │        │           │           │          │
      local      dev         int        prod
          │        │           │           │
     overrides overrides  overrides  overrides
     (vide)    (LLM local) (KC int)  (KC prod, LLM prod)
```

### Format du `dm-config.json`

Le fichier utilise une structure `default` + sections par environnement.
Les environnements sont **libres** — le developpeur peut en creer autant qu'il veut.

```json
{
  "configVersion": 1,
  "default": {
    "authHeaderName": "Authorization",
    "authHeaderPrefix": "Bearer ",
    "portal_url": "https://mirai.interieur.gouv.fr",
    "doc_url": "https://github.com/IA-Generative/AssistantMiraiLibreOffice/blob/master/docs/notice-utilisateur.md",
    "systemPrompt": "Tu es un assistant specialise...",
    "extend_selection_max_tokens": 15000,
    "extend_selection_system_prompt": "",
    "edit_selection_max_new_tokens": 15000,
    "edit_selection_system_prompt": "",
    "summarize_selection_max_tokens": 15000,
    "summarize_selection_system_prompt": "",
    "simplify_selection_max_tokens": 15000,
    "simplify_selection_system_prompt": "",
    "analyze_range_max_tokens": 4000,
    "llm_request_timeout_seconds": 45,
    "enabled": true,
    "telemetryEnabled": true,
    "telemetrylogJson": true
  },
  "local": {
    "llm_base_urls": "http://localhost:11434/api",
    "keycloakIssuerUrl": "http://localhost:8082/realms/openwebui",
    "keycloakRealm": "openwebui",
    "keycloakClientId": "bootstrap-mirai-lo-dev"
  },
  "dev": {
    "llm_base_urls": "http://localhost:11434/api"
  },
  "int": {
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}"
  },
  "prod": {
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}"
  },
  "dgx": {
    "llm_base_urls": "${{LLM_BASE_URL}}"
  }
}
```

**Regles** :
- `default` contient les valeurs communes a tous les environnements
- Chaque section environnement contient **seulement** les valeurs qui different
- Le DM merge : `default` + section du profil demande
- Les champs non definis (ni dans default ni dans la section) restent absents — le plugin utilise ses defauts locaux
- Les champs vides (`""`) sont remplis par le serveur au runtime :
  `bootstrap_url`, `config_path`, `device_name`, `telemetryEndpoint`, `telemetryKey`

**Environnements libres** : pas de liste fermee. Le developpeur cree les sections
qu'il veut. L'admin peut en ajouter d'autres via les overrides catalogue.
`?profile=staging` fonctionne meme si `staging` n'est pas dans le `dm-config.json`
— le DM appliquera juste le `default` + les overrides catalogue.

### Environnements normalises (recommandation)

4 profils standards sont recommandes. Les developpeurs de plugins sont encourages
a utiliser ces noms pour une coherence inter-plugins.

| Profil | Intention | DM present ? | Reseau | LLM | Keycloak |
|--------|-----------|-------------|--------|-----|----------|
| `local` | Dev en autonomie sur le poste du developpeur | **Non** | localhost uniquement | Ollama local (`http://localhost:11434`) ou mock | Aucun ou Keycloak Docker local (`localhost:8082`) |
| `dev` | Dev avec DM Docker Compose local | Oui (localhost:3001) | localhost | Ollama ou Scaleway dev | Keycloak Docker (`localhost:8082`) |
| `int` | Integration / recette sur un cluster partage | Oui (serveur int) | Interne | Scaleway ou GPU partage | Keycloak int |
| `prod` | Production | Oui (serveur prod) | Internet / VPN | LLM production (Scaleway, GPU minint) | Keycloak production |

#### Valeurs par defaut recommandees

```json
{
  "configVersion": 1,
  "default": {
    "authHeaderName": "Authorization",
    "authHeaderPrefix": "Bearer ",
    "enabled": true,
    "telemetryEnabled": true,
    "telemetrylogJson": true,
    "telemetryAuthorizationType": "Bearer",
    "extend_selection_max_tokens": 15000,
    "edit_selection_max_new_tokens": 15000,
    "summarize_selection_max_tokens": 15000,
    "simplify_selection_max_tokens": 15000,
    "analyze_range_max_tokens": 4000,
    "llm_request_timeout_seconds": 45
  },
  "local": {
    "_description": "Developpement autonome, sans DM, sans Keycloak",
    "llm_base_urls": "http://localhost:11434/api",
    "llm_default_models": "llama3.2",
    "llm_api_tokens": "not-needed",
    "telemetryEnabled": false,
    "bootstrap_url": "",
    "config_path": ""
  },
  "dev": {
    "_description": "Dev avec DM Docker Compose local",
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}"
  },
  "int": {
    "_description": "Integration / recette (cluster partage)",
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}"
  },
  "prod": {
    "_description": "Production",
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}"
  }
}
```

**Conventions** :
- `${{VAR}}` = placeholder substitue au runtime par la variable d'env de la plateforme DM
- `local` a des valeurs en dur (pas de DM, pas de substitution)
- `dev`, `int`, `prod` utilisent les placeholders — la plateforme DM fournit les vraies valeurs selon l'env
- Les champs `_description` sont ignores par le DM (documentation inline)
- Le developpeur peut ajouter des profils supplementaires (`staging`, `dgx`, `preprod`)

**Avantage** : un developpeur qui clone le repo plugin et lance en `local`
a un LLM fonctionnel (Ollama) sans aucune configuration serveur.
Les environnements servis par le DM (`dev`, `int`, `prod`) heritent automatiquement
des valeurs de la plateforme grace aux placeholders `${{VAR}}`.

### Variables de plateforme disponibles

Ces placeholders sont substitues par le DM au runtime avec les variables d'env du serveur :

| Placeholder | Variable d'env | Exemple |
|-------------|---------------|---------|
| `${{LLM_BASE_URL}}` | `LLM_BASE_URL` | `https://api.scaleway.ai/.../v1` |
| `${{LLM_API_TOKEN}}` | `LLM_API_TOKEN` | `17f16fa6-...` |
| `${{DEFAULT_MODEL_NAME}}` | `DEFAULT_MODEL_NAME` | `deepseek-r1-distill-llama-70b` |
| `${{KEYCLOAK_ISSUER_URL}}` | `KEYCLOAK_ISSUER_URL` | `https://sso.domain/realms/openwebui` |
| `${{KEYCLOAK_REALM}}` | `KEYCLOAK_REALM` | `openwebui` |
| `${{KEYCLOAK_CLIENT_ID}}` | `KEYCLOAK_CLIENT_ID` | `bootstrap-iassistant` |
| `${{KEYCLOAK_REDIRECT_URI}}` | `KEYCLOAK_REDIRECT_URI` | `http://localhost:28443/callback` |
| `${{KEYCLOAK_ALLOWED_REDIRECT_URI}}` | `KEYCLOAK_ALLOWED_REDIRECT_URI` | `http://localhost:28443/callback` |
| `${{PUBLIC_BASE_URL}}` | `PUBLIC_BASE_URL` | `https://bootstrap.domain.name` |
| `${{TELEMETRY_SALT}}` | `TELEMETRY_SALT` | (secret) |
| `${{TELEMETRY_KEY}}` | `TELEMETRY_KEY` | (secret) |

Le DM injecte aussi automatiquement (pas besoin de placeholder) :
- `bootstrap_url` → `PUBLIC_BASE_URL`
- `config_path` → `/config/{slug}/config.json`
- `device_name` → slug du plugin
- `telemetryEndpoint` → genere par le DM
- `telemetryKey` → JWT court-duree genere par le DM

### Initialisation automatique des placeholders

Quand un `dm-config.json` est enregistre (depuis le package ou upload separe),
le DM **complete automatiquement** les sections `dev`, `int` et `prod` avec les
placeholders de la plateforme. Cela garantit que meme un `dm-config.json` minimal
herite des variables serveur.

**Regles d'initialisation** :

| Cle | Placeholder | Ajoutee si absente ? | Remplacee si vide `""` ? |
|-----|------------|---------------------|-------------------------|
| `llm_base_urls` | `${{LLM_BASE_URL}}` | Oui | Oui |
| `llm_default_models` | `${{DEFAULT_MODEL_NAME}}` | Oui | Oui |
| `llm_api_tokens` | `${{LLM_API_TOKEN}}` | Oui | Oui |
| `keycloakIssuerUrl` | `${{KEYCLOAK_ISSUER_URL}}` | Oui | Oui |
| `keycloakRealm` | `${{KEYCLOAK_REALM}}` | Oui | Oui |
| `keycloakClientId` | `${{KEYCLOAK_CLIENT_ID}}` | Oui | Oui |
| `keycloak_redirect_uri` | `${{KEYCLOAK_REDIRECT_URI}}` | Oui | Oui |
| `keycloak_allowed_redirect_uri` | `${{KEYCLOAK_ALLOWED_REDIRECT_URI}}` | Oui | Oui |

**Ne s'applique pas a** la section `local` (pas de DM, valeurs en dur).
**Ne remplace pas** une valeur deja remplie (ex: `"llm_base_urls": "https://custom.api/v1"` est preservee).

```python
# Placeholders plateforme par defaut
_PLATFORM_DEFAULTS = {
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}",
}

# Sections qui ne recoivent PAS les placeholders
_LOCAL_PROFILES = {"local"}


def _apply_platform_defaults(template: dict) -> dict:
    """Complete les sections serveur avec les placeholders plateforme.

    - Ajoute les cles manquantes
    - Remplace les valeurs vides ("") par le placeholder
    - Ne touche pas aux sections 'local' ni aux valeurs deja remplies
    """
    for section_name, section in template.items():
        if section_name in ("configVersion", "default") or section_name in _LOCAL_PROFILES:
            continue
        if not isinstance(section, dict):
            continue
        for key, placeholder in _PLATFORM_DEFAULTS.items():
            current = section.get(key)
            if current is None or current == "":
                section[key] = placeholder
    return template
```

**Affichage dans l'admin UI** :

Quand le template est affiche dans l'onglet Configuration, les placeholders
sont mis en evidence avec un style distinct :

```html
<!-- Valeur plateforme (placeholder) -->
<span class="dm-placeholder" title="Sera remplace par la variable d'env LLM_BASE_URL">
  ${{LLM_BASE_URL}}
</span>

<!-- Valeur en dur (definie par le developpeur) -->
<span>http://localhost:11434/api</span>
```

```css
.dm-placeholder {
  color: #000091;
  background: #e8e8ff;
  padding: 1px 6px;
  border-radius: 3px;
  font-family: monospace;
  font-size: 0.85em;
  cursor: help;
}
```

Dans l'editeur JSON, un encart explique :

```
┌──────────────────────────────────────────────────────────────────┐
│  ℹ Les valeurs ${{...}} sont des variables de la plateforme.    │
│  Elles seront remplacees au runtime par les variables d'env     │
│  du serveur Device Management.                                   │
│                                                                  │
│  Exemple : ${{LLM_BASE_URL}} → https://api.scaleway.ai/.../v1  │
│                                                                  │
│  Les sections local restent en valeurs directes (pas de DM).    │
└──────────────────────────────────────────────────────────────────┘
```

### Pipeline de merge au serving

```python
def _build_config_from_template(template: dict, profile: str) -> dict:
    """Merge default + profile section from dm-config.json."""
    default = dict(template.get("default", {}))
    env_section = template.get(profile, {})
    # Profile overrides default
    merged = {**default, **env_section}
    return {"configVersion": template.get("configVersion", 1), "config": merged}
```

```
GET /config/mirai-libreoffice/config.json?profile=int

1. Charger plugins.config_template (dm-config.json stocke en DB)
2. Merger : default + section "int"
3. Injecter : bootstrap_url, config_path, device_name, telemetry*
4. Overrides catalogue : plugin_env_overrides WHERE environment='int'
5. Keycloak client : plugin_keycloak_clients WHERE environment='int'
6. Overrides DM : telemetrie, relay
7. Access control
8. Scrub secrets
```

### Nouveau modele de donnees

```sql
-- Deja dans le schema :
-- plugins.config_template JSONB
-- Le JSON complet du dm-config.json (avec default + sections env).
-- Les overrides catalogue (plugin_env_overrides) s'appliquent par-dessus.
-- Les environnements sont libres (VARCHAR(50), pas de CHECK constraint).
```

### Nouveau pipeline de configuration

```
1. RESOLVE    slug/alias → plugin_id, device_type
2. TEMPLATE   plugins.config_template (depuis la DB, plus depuis le disque)
              Fallback : config/{device_type}/config.{profile}.json (retrocompat)
3. INJECTION  bootstrap_url, config_path, device_name (forces par le serveur)
4. SYSTEME    Variables ${{VAR}} substituees
5. OVERRIDES  plugin_env_overrides (par profil)
6. KEYCLOAK   plugin_keycloak_clients (par profil)
7. DM         telemetrie, relay (existant)
8. ACCESS     open/waitlist/keycloak_group
9. SCRUB      secrets masques si pas de relay
```

La difference cle : le template vient de **la base** (colonne `config_template`),
pas du filesystem. Les fichiers `config/{device_type}/` deviennent un **fallback**
pour les plugins pas encore migres.

### Enregistrement d'un plugin (admin UI)

Quand l'admin uploade un package dans "Nouveau plugin" :

```
1. Extraire dm-config.json du ZIP
   → Si present : l'utiliser comme config_template
   → Si absent : generer un template par defaut base sur le device_type

2. Analyser le manifest (version, type, readme) — existant

3. Creer l'entree plugin en base avec config_template

4. Afficher le template dans l'onglet "Configuration" de la fiche
   → L'admin peut editer les valeurs par defaut
   → Les champs vides seront remplis par le serveur au runtime

5. Ajouter les overrides par environnement (dev/int/prod)
   → Keycloak client, LLM endpoint, etc.
```

### Template par defaut par device_type

Si le package ne contient pas de `dm-config.json`, le DM genere un template
de base selon le device_type :

```python
DEFAULT_CONFIG_TEMPLATES = {
    "libreoffice": {
        "configVersion": 1,
        "config": {
            "llm_base_urls": "", "llm_default_models": "", "llm_api_tokens": "",
            "authHeaderName": "Authorization", "authHeaderPrefix": "Bearer ",
            "keycloakIssuerUrl": "", "keycloakRealm": "", "keycloakClientId": "",
            "systemPrompt": "",
            "extend_selection_max_tokens": 15000, "extend_selection_system_prompt": "",
            "edit_selection_max_new_tokens": 15000, "edit_selection_system_prompt": "",
            "summarize_selection_max_tokens": 15000, "summarize_selection_system_prompt": "",
            "simplify_selection_max_tokens": 15000, "simplify_selection_system_prompt": "",
            "analyze_range_max_tokens": 4000, "llm_request_timeout_seconds": 45,
            "enabled": true, "telemetryEnabled": true, "telemetrylogJson": true,
        }
    },
    "matisse": {
        "configVersion": 1,
        "config": {
            # ... cles specifiques Thunderbird (systemPrompt, calendar, etc.)
        }
    },
}
```

### Nouvel onglet "Configuration" dans la fiche plugin

```
┌──────────────────────────────────────────────────────────────────────────┐
│  [Versions] [Changelog] [Configuration] [Environnements] [Keycloak]... │
│                                                                          │
│  Template de configuration                                               │
│                                                                          │
│  Ce template est la base de la config servie aux plugins.                │
│  Les champs vides sont remplis automatiquement par le serveur.          │
│  Les overrides par environnement (onglet suivant) s'appliquent          │
│  par-dessus.                                                             │
│                                                                          │
│  Source : [x] Extrait du package  [ ] Saisi manuellement                │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ {                                                                  │  │
│  │   "configVersion": 1,                                             │  │
│  │   "config": {                                                     │  │
│  │     "llm_base_urls": "",        ← rempli par le serveur          │  │
│  │     "systemPrompt": "Tu es...", ← valeur par defaut du plugin    │  │
│  │     "extend_selection_max_tokens": 15000,                         │  │
│  │     ...                                                           │  │
│  │   }                                                               │  │
│  │ }                                                                 │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  [ Editer ]  [ Reinitialiser depuis le package ]                        │
│                                                                          │
│  Apercu config finale :                                                  │
│  Profil [ dev ▼ ]  [ Previsualiser ]                                    │
└──────────────────────────────────────────────────────────────────────────┘
```

### Impact sur le deploiement du cluster

**Avant** (couple) :
```
Deployer DM = build image avec configs → deploy k8s → seed DB → plugins fonctionnent
Ajouter un plugin = modifier configs + ConfigMap + volumes + rebuild + redeploy
```

**Apres** (decouple) :
```
Deployer DM = deploy image generique → DM fonctionne (pas de plugin)
Ajouter un plugin = admin UI → upload package → fiche creee → overrides → fonctionne
                     (zero rebuild, zero redeploy)
```

### Migration des plugins existants

Pour les 2 plugins actuels (mirai-libreoffice, mirai-matisse) :

```python
# Script de migration one-shot :
# Lire le template depuis le fichier config, le stocker dans config_template
for slug in ['mirai-libreoffice', 'mirai-matisse']:
    plugin = get_plugin_by_slug(slug)
    template = load_config_template(profile='prod', device_type=plugin['device_type'],
                                    device_name=slug)
    cur.execute("UPDATE plugins SET config_template = %s WHERE slug = %s",
                (json.dumps(template), slug))
```

Apres migration, les fichiers `config/mirai-libreoffice/` et `config/matisse/`
deviennent le **fallback** et peuvent etre supprimes quand tous les plugins
sont migres.

### Suppression du ConfigMap k8s (objectif)

A terme, le ConfigMap `device-management-config` ne contient plus les configs
des plugins — seulement la config generique (`config.json`). Les volume mounts
par plugin disparaissent.

```yaml
# Avant (couple) :
volumes:
  - configMap:
      items:
        - key: config.json
          path: config.json
        - key: libreoffice-config.json    ← a supprimer
          path: mirai-libreoffice/config.json
        - key: matisse-config.json        ← a supprimer
          path: matisse/config.json

# Apres (decouple) :
volumes:
  - configMap:
      items:
        - key: config.json
          path: config.json              ← config generique seulement
```

### Nettoyage du binaire distribue

Quand `dm-config.json` est bundle dans le package (option A), le DM :
1. Extrait `dm-config.json` et le stocke dans `plugins.config_template`
2. **Retire le fichier du ZIP** avant de stocker l'artifact
3. L'artifact distribue aux utilisateurs ne contient plus `dm-config.json`

Cela evite que les valeurs par defaut (qui peuvent contenir des placeholders
ou des infos sur l'infrastructure) ne soient visibles dans le plugin installe.

```python
def _strip_dm_config_from_zip(data: bytes) -> bytes:
    """Remove dm-config.json from ZIP archive before storing."""
    import zipfile, io
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename.lower() in ('dm-config.json', 'dm_config.json'):
                continue
            dst.writestr(item, src.read(item.filename))
    return buf.getvalue()
```

### Convention `dm-config.json` dans le package plugin

Le developpeur du plugin peut ajouter `dm-config.json` a la racine de son archive,
ou le fournir separement lors de l'enregistrement.

Ce fichier est la **source de verite** pour les cles de configuration attendues.

Il sert a :
1. **Documenter** les cles que le plugin lit (contrat d'interface)
2. **Initialiser** le template dans le catalogue DM
3. **Fournir des valeurs par defaut** raisonnables
4. **Valider** que le DM sert toutes les cles attendues

### Interface d'upload dans le formulaire "Nouveau plugin"

```
┌──────────────────────────────────────────────────────────────────┐
│  1. Fichier du plugin *                                          │
│  [Choisir un fichier .oxt/.xpi]                                 │
│                                                                  │
│  2. Template de configuration (optionnel)                        │
│  [Choisir dm-config.json]                                        │
│  Si le package contient dm-config.json, il sera extrait          │
│  automatiquement. Sinon, un template par defaut sera genere.    │
│                                                                  │
│  [Analyser avec l'IA]                                            │
└──────────────────────────────────────────────────────────────────┘
```

### 4 environnements dans les overrides

Les CHECK constraints de la DB supportent 4 profils :

```sql
CHECK (environment IN ('local','dev','int','prod'))
```

| Profil | Config path appele par le plugin | Overrides typiques |
|--------|---|----|
| `local` | `?profile=local` | LLM localhost, KC localhost |
| `dev` | `?profile=dev` | LLM dev, KC dev |
| `int` | `?profile=int` | LLM int, KC int |
| `prod` | `?profile=prod` (ou defaut) | LLM prod, KC prod |

L'admin peut ajouter des overrides pour chaque profil dans l'onglet Environnements.

### Implementation dans l'endpoint suggest (existant)

L'endpoint `/admin/api/catalog/suggest` extrait deja les fichiers du ZIP.
Ajouter `dm-config.json` a la liste des fichiers interessants :

```python
interesting_files = {
    "manifest.json", "description.xml", "package.json",
    "readme.md", "notice-utilisateur.md", "changelog.md",
    "dm-config.json",  # ← NOUVEAU
}
```

Si `dm-config.json` est trouve, le stocker comme `config_template` dans la
reponse de suggest :

```json
{
  "name": "Assistant Mirai LibreOffice",
  "slug": "mirai-libreoffice",
  "config_template": { ... },   // ← extrait de dm-config.json
  "_has_config_template": true,
  ...
}
```

---

## Fichiers a modifier

| Fichier | Modification |
|---------|-------------|
| `db/schema.sql` | `ALTER TABLE plugins ADD COLUMN config_template JSONB` |
| `app/main.py` | `_load_config_template()` → chercher `plugins.config_template` d'abord |
| `app/admin/router.py` | Extraire `dm-config.json` dans suggest, onglet Configuration |
| `app/admin/services/catalog.py` | `update_plugin()` accepte `config_template` |
| `app/admin/templates/catalog_plugin.html` | Nouvel onglet Configuration |
| Plugin package (.oxt/.xpi) | Ajouter `dm-config.json` a la racine |

## Ordre d'implementation

1. Ajouter `config_template JSONB` a la table plugins
2. Modifier `_load_config_template()` : DB d'abord, fichier en fallback
3. Migrer les 2 plugins existants (copier le template file → DB)
4. Extraire `dm-config.json` dans l'endpoint suggest
5. Onglet Configuration dans la fiche plugin
6. Stocker `config_template` a la creation du plugin
7. Tester : creer un plugin via l'admin, verifier que la config est servie
8. Documenter la convention `dm-config.json` pour les developpeurs de plugins
9. A terme : supprimer les fichiers config/ et le ConfigMap k8s par plugin
