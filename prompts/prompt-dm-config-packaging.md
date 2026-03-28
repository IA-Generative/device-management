# Prompt — Inclusion du dm-config.json dans le packaging plugin

> Version : 1.0 — 2026-03-28
> Repos concernes : **AssistantMiraiLibreOffice**, **device-management**
> Objectif : ajouter `dm-config.json` dans le package .oxt du plugin LibreOffice,
> et adapter le DM pour l'extraire, le stocker en DB, et servir la config depuis la DB.

---

## Contexte

Le Device Management (DM) sert la configuration des plugins via l'endpoint
`GET /config/{device_name}/config.json?profile={env}`.

Aujourd'hui, les templates config sont stockes en fichiers sur disque (`config/mirai-libreoffice/`).
L'objectif est de les remplacer par un template fourni **dans le package du plugin** (`dm-config.json`),
stocke dans la colonne `plugins.config_template` (JSONB) en base.

## Fichiers cles a lire avant de commencer

| Repo | Fichier | Role |
|------|---------|------|
| device-management | `app/main.py` | API principale, `get_config()`, `_load_config_template()`, `_substitute_env()` |
| device-management | `app/admin/router.py` | Routes admin, endpoint `/admin/api/catalog/suggest` |
| device-management | `app/admin/services/catalog.py` | Service catalogue (CRUD plugins) |
| device-management | `db/schema.sql` | Schema DB (colonne `config_template JSONB` deja presente) |
| device-management | `config/mirai-libreoffice/config.dev.json` | Template actuel (fichier, a migrer) |
| device-management | `prompts/prompt-catalog-onboarding.md` | Spec complete du format dm-config.json |
| AssistantMiraiLibreOffice | `scripts/build-oxt.sh` ou equivalent | Script de build du package .oxt |

---

## Phase 1 — Cote plugin : creer et inclure dm-config.json

### 1.1 Creer le fichier `dm-config.json`

A la racine du repo AssistantMiraiLibreOffice, creer `dm-config.json` :

```json
{
  "configVersion": 1,
  "default": {
    "authHeaderName": "Authorization",
    "authHeaderPrefix": "Bearer ",
    "portal_url": "https://mirai.interieur.gouv.fr",
    "doc_url": "https://github.com/IA-Generative/AssistantMiraiLibreOffice/blob/master/docs/notice-utilisateur.md",
    "systemPrompt": "Tu es un assistant specialise dans la redaction de documents le contenu doit est concis et professionnels. Ton role est d'assister le redacteur pour disposer d'une redaction impecable. Ne repete pas mot pour mot le texte, reformule. Uniquement en texte seul et en francais",
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
    "telemetrylogJson": true,
    "telemetryAuthorizationType": "Bearer"
  },
  "local": {
    "_description": "Dev autonome, sans DM, sans Keycloak",
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
    "_description": "Integration / recette",
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

### 1.2 Inclure dans le build .oxt

Modifier le script de build pour inclure `dm-config.json` a la racine de l'archive .oxt.

Chercher le script de build (probable : `scripts/build-oxt.sh`, `Makefile`, ou `scripts/pack.sh`).
Ajouter `dm-config.json` a la liste des fichiers inclus dans le ZIP.

```bash
# Exemple (adapter au script existant) :
zip -r mirai-${VERSION}.oxt \
  META-INF/ \
  src/ \
  description.xml \
  manifest.xml \
  dm-config.json \
  ...
```

Verifier que le fichier est bien dans l'archive :
```bash
unzip -l mirai-*.oxt | grep dm-config
```

---

## Phase 2 — Cote DM : extraire dm-config.json a l'enregistrement

### 2.1 Modifier l'endpoint suggest pour extraire dm-config.json

Dans `app/admin/router.py`, l'endpoint `POST /admin/api/catalog/suggest` extrait
deja les fichiers du ZIP. Ajouter l'extraction de `dm-config.json` :

Chercher la liste `interesting_files` et ajouter `"dm-config.json"`.

Quand `dm-config.json` est trouve, le parser et l'inclure dans la reponse :

```python
# Dans la boucle d'extraction du ZIP :
if basename == "dm-config.json":
    try:
        config_template = json.loads(content)
        has_config_template = True
    except json.JSONDecodeError:
        pass
```

Ajouter dans la reponse JSON :
```python
suggestion["config_template"] = config_template if has_config_template else None
suggestion["_has_config_template"] = has_config_template
```

### 2.2 Auto-completer les placeholders plateforme

Apres extraction, appliquer `_apply_platform_defaults()` sur le template.
Cette fonction ajoute les placeholders `${{VAR}}` dans les sections serveur
(toute section sauf `local` et `default`) si les cles sont absentes ou vides.

```python
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
_LOCAL_PROFILES = {"local"}

def _apply_platform_defaults(template: dict) -> dict:
    for section_name, section in template.items():
        if section_name in ("configVersion", "default") or section_name in _LOCAL_PROFILES:
            continue
        if not isinstance(section, dict):
            continue
        for key, placeholder in _PLATFORM_DEFAULTS.items():
            if section.get(key) is None or section.get(key) == "":
                section[key] = placeholder
    return template
```

### 2.3 Stocker le template a la creation du plugin

Dans `POST /admin/deploy/create` et `POST /admin/catalog`, quand un plugin est cree :

1. Si `config_template` est dans les donnees du suggest → le stocker dans `plugins.config_template`
2. Sinon, si l'admin a uploade un `dm-config.json` separe → le parser et stocker
3. Sinon → generer un template par defaut base sur le device_type

```python
# Dans la route de creation :
config_template = suggestion.get("config_template")
if config_template:
    config_template = _apply_platform_defaults(config_template)

catalog_svc.update_plugin(cur, plugin_id, config_template=json.dumps(config_template))
```

### 2.4 Retirer dm-config.json du binaire distribue

Quand l'artifact est stocke, retirer `dm-config.json` du ZIP pour que les
utilisateurs finaux ne voient pas les placeholders :

```python
def _strip_dm_config_from_zip(data: bytes) -> bytes:
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

Appeler cette fonction dans `artifact_upload` et `deploy_create` **apres** l'extraction
du dm-config.json et **avant** le stockage sur disque/S3.

---

## Phase 3 — Cote DM : servir la config depuis la DB

### 3.1 Modifier `_load_config_template()` dans `app/main.py`

Ajouter le chargement depuis `plugins.config_template` comme source prioritaire.
Le merge `default` + section profil se fait a ce niveau.

```python
def _load_config_template(profile: str, device: str | None = None,
                          device_name: str | None = None,
                          plugin_id: int | None = None, cur=None) -> dict:
    # 1. DB: plugins.config_template (prioritaire)
    if plugin_id and cur:
        try:
            cur.execute("SELECT config_template FROM plugins WHERE id = %s", (plugin_id,))
            row = cur.fetchone()
            if row and row[0]:
                template = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                return _build_config_from_template(template, profile)
        except Exception:
            pass

    # 2. Fichier: config/{device_name}/config.{profile}.json (fallback)
    # ... (code existant inchange)
```

### 3.2 Implementer `_build_config_from_template()`

```python
def _build_config_from_template(template: dict, profile: str) -> dict:
    """Merge default + profile section from dm-config.json."""
    default = dict(template.get("default", {}))
    env_section = template.get(profile, {})
    if isinstance(env_section, dict):
        merged = {**default, **env_section}
    else:
        merged = default
    # Retirer les champs _description (documentation inline)
    merged.pop("_description", None)
    return {"configVersion": template.get("configVersion", 1), "config": merged}
```

### 3.3 Passer plugin_id et cur a `_load_config_template()`

Dans `get_config()`, mettre a jour l'appel :

```python
# Avant :
cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None)

# Apres (passer plugin_id et cur si disponibles) :
cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None,
                            plugin_id=plugin_id, cur=cur_if_available)
```

Note : il faut restructurer legerement `get_config()` pour avoir un `cur`
disponible au moment du chargement du template. Utiliser la meme connexion
que celle de `_resolve_device()`.

---

## Phase 4 — Migration des plugins existants

### 4.1 Script de migration one-shot

Lire les templates existants depuis les fichiers, les convertir en format dm-config.json,
et les stocker dans `plugins.config_template`.

```python
# A executer une fois sur le cluster :
import json, psycopg2

conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/bootstrap')
conn.autocommit = True
cur = conn.cursor()

# Lire les fichiers config existants et construire le template
for slug, device_type in [('mirai-libreoffice', 'libreoffice'), ('mirai-matisse', 'matisse')]:
    template = {"configVersion": 1, "default": {}, "local": {}, "dev": {}, "int": {}, "prod": {}}

    for profile in ['dev', 'int', 'prod']:
        # Charger le fichier
        import os
        for base in [f'/app/config/{slug}', f'/app/config/{device_type}']:
            for candidate in [f'{base}/config.{profile}.json', f'{base}/config.json']:
                if os.path.isfile(candidate):
                    with open(candidate) as f:
                        data = json.load(f)
                    cfg = data.get('config', data)
                    # Premiere lecture → remplir default
                    if not template['default']:
                        template['default'] = {k: v for k, v in cfg.items()
                                                if not k.startswith('_') and k not in
                                                ('bootstrap_url', 'config_path', 'device_name',
                                                 'telemetryEndpoint', 'telemetryKey')}
                    # Section profil → seulement les differences
                    diff = {}
                    for k, v in cfg.items():
                        if k.startswith('_'): continue
                        if template['default'].get(k) != v:
                            diff[k] = v
                    if diff:
                        template[profile] = diff
                    break
            else:
                continue
            break

    # Local : Ollama, pas de DM
    template['local'] = {
        '_description': 'Dev autonome, sans DM',
        'llm_base_urls': 'http://localhost:11434/api',
        'llm_default_models': 'llama3.2',
        'llm_api_tokens': 'not-needed',
        'telemetryEnabled': False,
        'bootstrap_url': '', 'config_path': '',
    }

    # Appliquer les placeholders plateforme
    # (importer _apply_platform_defaults ou l'inliner)

    cur.execute("UPDATE plugins SET config_template = %s WHERE slug = %s",
                (json.dumps(template), slug))
    print(f'{slug}: config_template mis a jour ({len(json.dumps(template))} bytes)')

conn.close()
```

---

## Phase 5 — Nouvel onglet "Configuration" dans la fiche plugin admin

### 5.1 Affichage du template

Dans `app/admin/templates/catalog_plugin.html`, ajouter un onglet "Configuration"
qui affiche le `dm-config.json` stocke dans `plugins.config_template` :

- Editeur JSON avec coloration syntaxique (ou `<textarea>` avec monospace)
- Les placeholders `${{VAR}}` affiches en bleu sur fond clair
- Bouton "Reinitialiser depuis le package" (re-extrait du dernier artifact uploade)
- Bouton "Previsualiser" par profil (appelle `/admin/api/catalog/{id}/preview?profile=dev`)
- Indication de la source : "Extrait du package" ou "Saisi manuellement"

### 5.2 Route d'edition

```python
@router.post("/catalog/{plugin_id}/config-template")
@require_admin
async def catalog_update_config_template(request: Request, plugin_id: int,
                                          config_template: str = Form(...)):
    template = json.loads(config_template)
    template = _apply_platform_defaults(template)
    # ... update_plugin(cur, plugin_id, config_template=json.dumps(template))
```

---

## Verification

### Test 1 : dm-config.json dans le package
```bash
# Cote plugin
cd AssistantMiraiLibreOffice
# Build le .oxt
scripts/build-oxt.sh
# Verifier que dm-config.json est dans l'archive
unzip -l dist/mirai-*.oxt | grep dm-config
```

### Test 2 : extraction par le DM
```bash
# Upload le .oxt dans l'admin /admin/catalog/new
# Verifier que le template est extrait et affiche
# Verifier que les placeholders ${{VAR}} sont presents dans dev/int/prod
```

### Test 3 : serving depuis la DB
```bash
# Apres enregistrement du plugin :
curl -s 'http://localhost:3001/config/mirai-libreoffice/config.json?profile=dev' | python3 -c "
import sys, json
d = json.load(sys.stdin)
cfg = d.get('config', d)
# Les placeholders doivent etre substitues par les vraies valeurs
assert cfg.get('llm_base_urls') != '\${{LLM_BASE_URL}}', 'Placeholder non substitue'
assert cfg.get('device_name') == 'mirai-libreoffice', 'device_name incorrect'
print('OK: config servie depuis la DB')
"
```

### Test 4 : profil local
```bash
curl -s 'http://localhost:3001/config/mirai-libreoffice/config.json?profile=local' | python3 -c "
import sys, json
d = json.load(sys.stdin)
cfg = d.get('config', d)
assert cfg.get('llm_base_urls') == 'http://localhost:11434/api', 'local doit avoir Ollama'
assert cfg.get('telemetryEnabled') == False, 'local doit desactiver la telemetrie'
print('OK: profil local fonctionne')
"
```

### Test 5 : binaire nettoye
```bash
# Verifier que l'artifact stocke ne contient plus dm-config.json
# (le fichier a ete retire avant stockage)
```

---

## Resume des fichiers a modifier

| Repo | Fichier | Action |
|------|---------|--------|
| AssistantMiraiLibreOffice | `dm-config.json` | Creer a la racine du repo |
| AssistantMiraiLibreOffice | Script de build .oxt | Inclure dm-config.json dans l'archive |
| device-management | `app/admin/router.py` | Extraire dm-config.json dans suggest, stocker a la creation, strip du binaire |
| device-management | `app/main.py` | `_load_config_template()` : DB d'abord, `_build_config_from_template()` pour le merge |
| device-management | `app/admin/services/catalog.py` | `update_plugin()` accepte config_template |
| device-management | `app/admin/templates/catalog_plugin.html` | Onglet Configuration |

## Ordre d'implementation

1. Creer `dm-config.json` dans le repo plugin
2. Inclure dans le build .oxt
3. Modifier suggest pour extraire `dm-config.json` + `_apply_platform_defaults()`
4. Modifier `_load_config_template()` pour charger depuis la DB
5. Implementer `_build_config_from_template()` (merge default + profil)
6. Implementer `_strip_dm_config_from_zip()` dans l'upload artifact
7. Stocker `config_template` a la creation du plugin
8. Migrer les 2 plugins existants (script one-shot)
9. Ajouter l'onglet Configuration dans la fiche plugin admin
10. Tester les 5 scenarios de verification
11. A terme : supprimer les fichiers `config/mirai-libreoffice/` et le ConfigMap k8s
