# Guide de packaging des plugins pour Device Management

> **Étape 3 du parcours développeur** ([README](README.md)) : préparer une archive que DM sait
> détecter, décrire et publier automatiquement. Ce que ces fichiers changent pour le *protocole*
> est traité au § 2 bis du [contrat d'interface](plugin-dm-protocol-update-features.md).

---

## Principe

Chaque plugin est distribué sous forme d'archive ZIP (renommée selon la plateforme : .oxt, .xpi, .crx). DM détecte automatiquement les métadonnées du plugin à partir de deux fichiers optionnels placés **a la racine** de l'archive :

> **Ces deux fichiers ne partent jamais sur les postes.** À la publication, DM les extrait, en
> ingère le contenu, puis **reconstruit l'archive sans eux** avant de la stocker. Le `checksum`
> distribué porte donc sur l'archive *après* retrait : il ne correspondra pas à l'empreinte du
> paquet que vous avez construit. Ce retrait n'opère que sur les fichiers placés **a la racine** —
> un `dm-*.json` imbriqué serait lu, mais publié dans le binaire.

| Fichier | Rôle | Obligatoire |
|---------|------|-------------|
| `dm-manifest.json` | Fiche catalogue (nom, description, changelog, features) | Recommandé |
| `dm-config.json` | Template de configuration par environnement | Recommandé |

---

## dm-manifest.json — Fiche catalogue

Ce fichier décrit le plugin pour le catalogue. Tous les champs sont optionnels sauf `slug` et `name`.

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
| `name` | string | Nom affiché dans le catalogue |
| `description` | string | Description longue |
| `intent` | string | Proposition de valeur courte (1-2 phrases) |
| `device_type` | string | `libreoffice`, `matisse`, `firefox`, `chrome`, `edge` |
| `category` | string | `productivity`, `security`, `communication`, `tools` |
| `publisher` | string | Éditeur / équipe |
| `visibility` | string | `public`, `internal`, `hidden` |
| `homepage_url` | string | URL du projet |
| `support_email` | string | Email de support |
| `icon_url` | string | Chemin relatif vers l'icône dans l'archive (ex: `assets/logo.png`) |
| `doc_url` | string | URL de la documentation |
| `license` | string | Licence (SPDX) |
| `key_features` | array | Liste de fonctionnalités clés (affichées comme tags) |
| `changelog` | array | Historique des versions (la plus récente en premier) |

### Icône

L'icône doit être un PNG (recommandé 128x128 ou 256x256). DM la cherche dans cet ordre :

1. Le chemin indiqué dans `dm-manifest.json` → `icon_url` (ex: `assets/logo.png`)
2. `assets/logo.png`
3. `icons/icon128.png`
4. `icons/icon48.png`

L'icône est stockée en base (data URL base64) — pas de fichier sur disque.

---

## dm-config.json — Template de configuration

Ce fichier définit la configuration servie aux plugins par DM. Il est structuré en sections : une section `default` + une section par environnement.

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
    "authHeaderPrefix": "Bearer ",
    "featureToggles": {
      "composePromptPanel": true,
      "search": true
    }
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

### Règles

- `default` : valeurs communes à tous les profils
- `local` : mode autonome sans DM (dev sur le poste)
- `dev`, `int`, `prod` : overrides par environnement (fusionnés avec `default`)
- `${{VAR}}` : placeholders substitués par le serveur DM au runtime
- Les champs `_description` sont informatifs et retirés de la config servie
- Les sections serveur (`dev`, `int`, `prod`) sont **auto-complétées** par DM avec les placeholders si vous ne les fournissez pas

### `featureToggles` — les défauts des feature flags

`featureToggles` est **l'autorité des valeurs par défaut** des feature flags : ni le catalogue DM
ni l'IHM d'administration ne les remplacent. L'administrateur ne pose que des **overrides de
cohorte** par-dessus ; pour changer un défaut, il faut republier le paquet.

Trois règles spécifiques :

- **Deep-merge**, contrairement au reste de la section : `featureToggles` de `default` et du profil
  sont fusionnés clé par clé. Un profil qui surcharge un flag n'efface pas les autres.
- À chaque publication, DM **réconcilie son catalogue de flags** avec l'union des clés
  `featureToggles` de tous les profils (`default` inclus).
- Un flag retiré du gabarit est marqué **orphelin** (`deprecated`) : il cesse d'être diffusé, mais
  n'est jamais supprimé automatiquement.

Résolution complète côté serveur et contrat de consommation côté plugin :
[plugin-dm-protocol-update-features.md](plugin-dm-protocol-update-features.md) § 4.4.

### Placeholders disponibles

| Placeholder | Variable serveur | Description |
|-------------|-----------------|-------------|
| `${{LLM_BASE_URL}}` | `LLM_BASE_URL` | Endpoint LLM |
| `${{LLM_API_TOKEN}}` | `LLM_API_TOKEN` | Token API LLM (secret, scrubbed sans relay) |
| `${{DEFAULT_MODEL_NAME}}` | `DEFAULT_MODEL_NAME` | Modèle LLM par défaut |
| `${{KEYCLOAK_ISSUER_URL}}` | `KEYCLOAK_ISSUER_URL` | URL issuer Keycloak |
| `${{KEYCLOAK_REALM}}` | `KEYCLOAK_REALM` | Realm Keycloak |
| `${{KEYCLOAK_CLIENT_ID}}` | `KEYCLOAK_CLIENT_ID` | Client ID Keycloak |
| `${{KEYCLOAK_REDIRECT_URI}}` | `KEYCLOAK_REDIRECT_URI` | URI de redirect OAuth |
| `${{KEYCLOAK_ALLOWED_REDIRECT_URI}}` | `KEYCLOAK_ALLOWED_REDIRECT_URI` | URI de redirect autorisée |
| `${{PUBLIC_BASE_URL}}` | `PUBLIC_BASE_URL` | URL publique de DM |

---

## Détection automatique

Quand un fichier est uploadé dans DM, le système détecte automatiquement :

### Version

| Priorité | Source | Méthode |
|----------|--------|---------|
| 1 | `manifest.json` | Champ `version` (WebExtension) |
| 2 | `description.xml` | `<version value="...">` (OXT LibreOffice) |
| 3 | `dm-manifest.json` | Première entrée du `changelog` |
| 4 | Nom du fichier | Regex `(\d+\.\d+(?:\.\d+)*)` |

### Type de plugin

| Extension | Condition | `device_type` |
|-----------|-----------|---------------|
| `.oxt` | — | `libreoffice` |
| `.xpi` | `browser_specific_settings.thunderbird` dans manifest | `matisse` |
| `.xpi` | `browser_specific_settings.gecko` ou par défaut | `firefox` |
| `.crx` | — | `chrome` |
| `.crx` | `manifest_version: 3` sans gecko | `chrome` ou `edge` |

### Icône

Voir [Icône](#icône) sous `dm-manifest.json` — même ordre de recherche.

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

### Edge (.crx, identique à Chrome)

Edge utilise le même format que Chrome (Manifest V3). Le packaging est identique.

La seule différence : pour la distribution via le Edge Add-ons Store, soumettre sur [partner.microsoft.com/dashboard/microsoftedge](https://partner.microsoft.com/dashboard/microsoftedge).

Pour la distribution via DM, le `.crx` ou `.zip` est identique au format Chrome.

---

## Upload dans Device Management

### Via l'admin UI

1. Aller dans `/admin/catalog/new`
2. Sélectionner le fichier (.oxt, .xpi, .crx)
3. DM analyse le package : détecte version, type, extrait dm-manifest.json, dm-config.json, icône
4. Vérifier et compléter la fiche
5. Valider — le plugin est créé avec la version publiée

### Via le script de déploiement

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

## Vérification

Après upload, vérifier :

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
