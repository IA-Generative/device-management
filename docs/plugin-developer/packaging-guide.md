# Guide de packaging des plugins pour Device Management

> Ce document explique comment preparer un plugin pour qu'il soit automatiquement detecte et enregistre dans le catalogue Device Management (DM).

---

## Principe

Chaque plugin est distribue sous forme d'archive ZIP (renommee selon la plateforme : .oxt, .xpi, .crx). DM detecte automatiquement les metadonnees du plugin a partir de deux fichiers optionnels places **a la racine** de l'archive :

| Fichier | Role | Obligatoire |
|---------|------|-------------|
| `dm-manifest.json` | Fiche catalogue (nom, description, changelog, features) | Recommande |
| `dm-config.json` | Template de configuration par environnement | Recommande |

Ces fichiers sont **retires automatiquement** du binaire distribue aux utilisateurs finaux — ils ne servent qu'a DM lors de l'upload.

---

## dm-manifest.json — Fiche catalogue

Ce fichier decrit le plugin pour le catalogue. Tous les champs sont optionnels sauf `slug` et `name`.

```json
{
  "slug": "mon-plugin",
  "name": "Mon Plugin — Description Courte",
  "description": "Description detaillee du plugin et de ses fonctionnalites.",
  "intent": "Proposition de valeur en 1-2 phrases pour les utilisateurs.",
  "device_type": "libreoffice",
  "category": "productivity",
  "publisher": "DTNUM",
  "visibility": "public",
  "homepage_url": "https://github.com/mon-org/mon-plugin",
  "support_email": "support@example.com",
  "icon_url": "assets/logo.png",
  "doc_url": "https://github.com/mon-org/mon-plugin/blob/main/docs/notice.md",
  "license": "MPL-2.0",
  "key_features": [
    "Fonctionnalite 1",
    "Fonctionnalite 2",
    "Fonctionnalite 3"
  ],
  "changelog": [
    {
      "version": "1.1.0",
      "date": "2026-03-15",
      "changes": [
        "Nouvelle fonctionnalite X",
        "Correction du bug Y"
      ]
    },
    {
      "version": "1.0.0",
      "date": "2026-01-01",
      "changes": [
        "Premiere version"
      ]
    }
  ]
}
```

### Champs

| Champ | Type | Description |
|-------|------|-------------|
| `slug` | string | Identifiant unique du plugin (minuscules, tirets). Ex: `mirai-libreoffice` |
| `name` | string | Nom affiche dans le catalogue |
| `description` | string | Description longue |
| `intent` | string | Proposition de valeur courte (1-2 phrases) |
| `device_type` | string | `libreoffice`, `matisse`, `firefox`, `chrome`, `edge` |
| `category` | string | `productivity`, `security`, `communication`, `tools` |
| `publisher` | string | Editeur / equipe |
| `visibility` | string | `public`, `internal`, `hidden` |
| `homepage_url` | string | URL du projet |
| `support_email` | string | Email de support |
| `icon_url` | string | Chemin relatif vers l'icone dans l'archive (ex: `assets/logo.png`) |
| `doc_url` | string | URL de la documentation |
| `license` | string | Licence (SPDX) |
| `key_features` | array | Liste de fonctionnalites cles (affichees comme tags) |
| `changelog` | array | Historique des versions (la plus recente en premier) |

### Icone

L'icone doit etre un PNG (recommande 128x128 ou 256x256). DM la cherche dans cet ordre :

1. Le chemin indique dans `dm-manifest.json` → `icon_url` (ex: `assets/logo.png`)
2. `assets/logo.png`
3. `icons/icon128.png`
4. `icons/icon48.png`

L'icone est stockee en base (data URL base64) — pas de fichier sur disque.

---

## dm-config.json — Template de configuration

Ce fichier definit la configuration servie aux plugins par DM. Il est structure en sections : une section `default` + une section par environnement.

```json
{
  "configVersion": 1,
  "default": {
    "enabled": true,
    "systemPrompt": "Tu es un assistant...",
    "telemetryEnabled": true,
    "telemetrylogJson": true,
    "telemetryAuthorizationType": "Bearer",
    "authHeaderName": "Authorization",
    "authHeaderPrefix": "Bearer "
  },
  "local": {
    "_description": "Dev autonome, sans DM, sans Keycloak",
    "config_path": "",
    "bootstrap_url": "",
    "llm_base_urls": "http://localhost:11434/api",
    "llm_api_tokens": "not-needed",
    "llm_default_models": "llama3.2",
    "telemetryEnabled": false
  },
  "dev": {
    "_description": "Dev avec DM Docker Compose local",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}"
  },
  "int": {
    "_description": "Integration / recette"
  },
  "prod": {
    "_description": "Production"
  }
}
```

### Regles

- `default` : valeurs communes a tous les profils
- `local` : mode autonome sans DM (dev sur le poste)
- `dev`, `int`, `prod` : overrides par environnement (merges avec `default`)
- `${{VAR}}` : placeholders substitues par le serveur DM au runtime
- Les champs `_description` sont informatifs et retires de la config servie
- Les sections serveur (`dev`, `int`, `prod`) sont **auto-completees** par DM avec les placeholders si vous ne les fournissez pas

### Placeholders disponibles

| Placeholder | Variable serveur | Description |
|-------------|-----------------|-------------|
| `${{LLM_BASE_URL}}` | `LLM_BASE_URL` | Endpoint LLM |
| `${{LLM_API_TOKEN}}` | `LLM_API_TOKEN` | Token API LLM (secret, scrubbed sans relay) |
| `${{DEFAULT_MODEL_NAME}}` | `DEFAULT_MODEL_NAME` | Modele LLM par defaut |
| `${{KEYCLOAK_ISSUER_URL}}` | `KEYCLOAK_ISSUER_URL` | URL issuer Keycloak |
| `${{KEYCLOAK_REALM}}` | `KEYCLOAK_REALM` | Realm Keycloak |
| `${{KEYCLOAK_CLIENT_ID}}` | `KEYCLOAK_CLIENT_ID` | Client ID Keycloak |
| `${{KEYCLOAK_REDIRECT_URI}}` | `KEYCLOAK_REDIRECT_URI` | URI de redirect OAuth |
| `${{KEYCLOAK_ALLOWED_REDIRECT_URI}}` | `KEYCLOAK_ALLOWED_REDIRECT_URI` | URI de redirect autorisee |
| `${{PUBLIC_BASE_URL}}` | `PUBLIC_BASE_URL` | URL publique de DM |

---

## Detection automatique

Quand un fichier est uploade dans DM, le systeme detecte automatiquement :

### Version

| Priorite | Source | Methode |
|----------|--------|---------|
| 1 | `manifest.json` | Champ `version` (WebExtension) |
| 2 | `description.xml` | `<version value="...">` (OXT LibreOffice) |
| 3 | `dm-manifest.json` | Premiere entree du `changelog` |
| 4 | Nom du fichier | Regex `(\d+\.\d+(?:\.\d+)*)` |

### Type de plugin

| Extension | Condition | `device_type` |
|-----------|-----------|---------------|
| `.oxt` | — | `libreoffice` |
| `.xpi` | `browser_specific_settings.thunderbird` dans manifest | `matisse` |
| `.xpi` | `browser_specific_settings.gecko` ou par defaut | `firefox` |
| `.crx` | — | `chrome` |
| `.crx` | `manifest_version: 3` sans gecko | `chrome` ou `edge` |

### Icone

Recherchee dans l'archive :
1. Chemin `icon_url` du `dm-manifest.json`
2. `assets/logo.png`
3. `icons/icon128.png`, `icons/icon48.png`

---

## Packaging par plateforme

### LibreOffice (.oxt)

```
mon-plugin.oxt (ZIP)
├── dm-manifest.json          ← catalogue DM
├── dm-config.json            ← config DM
├── description.xml           ← manifest OXT (version)
├── META-INF/
│   └── manifest.xml          ← declaration des composants
├── assets/
│   └── logo.png              ← icone (128x128 recommande)
├── Addons.xcu                ← menus et barres d'outils
├── *.xba, *.xdl, *.xlb      ← macros Basic
└── ...                       ← autres fichiers du plugin
```

**description.xml** (version) :
```xml
<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006">
  <identifier value="com.example.mon-plugin"/>
  <version value="1.2.0"/>
  <display-name><name lang="fr">Mon Plugin</name></display-name>
</description>
```

**Build** :
```bash
cd oxt/
zip -r ../dist/mon-plugin.oxt . -x "*.DS_Store" "__MACOSX/*"
```

---

### Thunderbird Legacy (.xpi, TB60-68)

```
mon-plugin.xpi (ZIP)
├── dm-manifest.json          ← catalogue DM
├── dm-config.json            ← config DM
├── install.rdf               ← manifest legacy (version, ID)
├── chrome.manifest           ← enregistrement chrome
├── bootstrap.js              ← point d'entree
├── assets/
│   └── logo.png              ← icone
├── modules/
│   ├── plugin-state.js       ← gestion d'etat
│   ├── api.js                ← appels LLM
│   └── ...
├── chrome/
│   ├── content/              ← XUL dialogs
│   └── skin/                 ← CSS
└── defaults/
    └── preferences/
        └── prefs.js           ← preferences par defaut
```

**install.rdf** (version) :
```xml
<?xml version="1.0" encoding="UTF-8"?>
<RDF xmlns="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
     xmlns:em="http://www.mozilla.org/2004/em-rdf#">
  <Description about="urn:mozilla:install-manifest">
    <em:id>mon-plugin@example.com</em:id>
    <em:version>0.8.0</em:version>
    <em:type>2</em:type>
    <em:name>Mon Plugin</em:name>
  </Description>
</RDF>
```

**Build** :
```bash
cd thunderbird/
zip -r ../dist/mon-plugin.xpi . -x "*.DS_Store" "docs/*" "dist/*" "tests/*"
```

---

### Thunderbird WebExtension (.xpi, TB128+)

```
mon-plugin.xpi (ZIP)
├── dm-manifest.json          ← catalogue DM
├── dm-config.json            ← config DM
├── manifest.json             ← manifest WebExtension
├── background.js             ← service worker / background script
├── assets/
│   └── logo.png              ← icone
├── popup/
│   ├── popup.html
│   ├── popup.js
│   └── popup.css
└── icons/
    ├── icon48.png
    └── icon128.png
```

**manifest.json** :
```json
{
  "manifest_version": 2,
  "name": "Mon Plugin",
  "version": "1.0.0",
  "browser_specific_settings": {
    "gecko": {
      "id": "mon-plugin@example.com",
      "strict_min_version": "128.0"
    },
    "thunderbird": {
      "strict_min_version": "128.0"
    }
  },
  "permissions": ["storage", "tabs"],
  "background": {
    "scripts": ["background.js"]
  }
}
```

**Build** :
```bash
zip -r ../dist/mon-plugin.xpi . -x "*.DS_Store" "node_modules/*" "docs/*"
```

---

### Chrome / Chromium (.crx ou .zip)

```
mon-plugin/ (ZIP ou .crx)
├── dm-manifest.json          ← catalogue DM
├── dm-config.json            ← config DM
├── manifest.json             ← manifest WebExtension MV3
├── background.js             ← service worker
├── popup.html
├── popup.js
├── popup.css
├── options.html
├── options.js
└── icons/
    ├── icon16.png
    ├── icon48.png
    └── icon128.png
```

**manifest.json** :
```json
{
  "manifest_version": 3,
  "name": "Mon Plugin",
  "version": "1.2.1",
  "description": "Description du plugin",
  "permissions": ["tabs", "storage", "identity", "alarms"],
  "host_permissions": ["<all_urls>"],
  "background": {
    "service_worker": "background.js",
    "type": "module"
  },
  "action": {
    "default_popup": "popup.html",
    "default_icon": {
      "16": "icons/icon16.png",
      "48": "icons/icon48.png",
      "128": "icons/icon128.png"
    }
  },
  "icons": {
    "16": "icons/icon16.png",
    "48": "icons/icon48.png",
    "128": "icons/icon128.png"
  }
}
```

**Build** :
```bash
zip -r ../dist/mon-plugin.zip . -x "*.DS_Store" "*.crx" "*.git*" "node_modules/*"
# Ou packager en .crx via chrome://extensions en mode developpeur
```

---

### Firefox (.xpi, MV2 ou MV3)

```
mon-plugin.xpi (ZIP)
├── dm-manifest.json          ← catalogue DM
├── dm-config.json            ← config DM
├── manifest.json             ← manifest WebExtension
├── background.js
├── popup/
│   ├── popup.html
│   ├── popup.js
│   └── popup.css
└── icons/
    ├── icon48.png
    └── icon128.png
```

**manifest.json** :
```json
{
  "manifest_version": 2,
  "name": "Mon Plugin",
  "version": "1.0.0",
  "browser_specific_settings": {
    "gecko": {
      "id": "mon-plugin@example.com",
      "strict_min_version": "128.0"
    }
  },
  "permissions": ["tabs", "storage"],
  "background": {
    "scripts": ["background.js"]
  },
  "browser_action": {
    "default_popup": "popup/popup.html",
    "default_icon": {
      "48": "icons/icon48.png",
      "128": "icons/icon128.png"
    }
  }
}
```

**Build** :
```bash
cd firefox/
zip -r ../dist/mon-plugin.xpi . -x "*.DS_Store" "node_modules/*"
# Ou soumettre sur addons.mozilla.org pour signature
```

---

### Edge (.crx, identique a Chrome)

Edge utilise le meme format que Chrome (Manifest V3). Le packaging est identique.

La seule difference : pour la distribution via le Edge Add-ons Store, soumettre sur [partner.microsoft.com/dashboard/microsoftedge](https://partner.microsoft.com/dashboard/microsoftedge).

Pour la distribution via DM, le `.crx` ou `.zip` est identique au format Chrome.

---

## Upload dans Device Management

### Via l'admin UI

1. Aller dans `/admin/catalog/new`
2. Selectionner le fichier (.oxt, .xpi, .crx)
3. DM analyse le package : detecte version, type, extrait dm-manifest.json, dm-config.json, icone
4. Verifier et completer la fiche
5. Valider — le plugin est cree avec la version publiee

### Via le script de deploiement

```bash
export DM_ADMIN_TOKEN="votre-token"
curl -X POST https://bootstrap.example.com/api/plugins/mon-plugin/deploy \
  -H "X-Admin-Token: $DM_ADMIN_TOKEN" \
  -F "binary=@dist/mon-plugin.oxt" \
  -F "strategy=canary"
```

### Via le script deploy-release.sh

```bash
export DM_ADMIN_TOKEN="votre-token"
./scripts/deploy-release.sh \
  --bootstrap-url https://bootstrap.example.com \
  --strategy canary
```

---

## Verification

Apres upload, verifier :

```bash
# Fiche catalogue
curl -s https://bootstrap.example.com/catalog/api/plugins/mon-plugin | python3 -m json.tool

# Config servie
curl -s https://bootstrap.example.com/config/mon-plugin/config.json?profile=int | python3 -m json.tool

# Telechargement
curl -LO https://bootstrap.example.com/catalog/mon-plugin/download

# Icone
curl -s -o icon.png https://bootstrap.example.com/catalog/api/plugins/mon-plugin/icon.png
```
