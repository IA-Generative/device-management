# Prompt — Integrer un plugin avec Device Management (DM)

> Version : 1.0 — 2026-03-31
> Objectif : Modifier la base de code d'un plugin existant pour l'integrer avec Device Management
> Prerequis : un serveur DM deploye avec le catalogue fonctionnel

---

## Contexte

Device Management (DM) est une plateforme de gestion de plugins qui offre :
- **Configuration centralisee** : le plugin recupere sa config au demarrage via une API REST
- **Mises a jour automatiques** : DM notifie le plugin quand une nouvelle version est disponible
- **Telemetrie** : le plugin envoie des traces d'usage (OpenTelemetry) vers DM
- **Enrollment** : le plugin s'enregistre aupres de DM pour recevoir des secrets (tokens LLM, etc.)

Ce prompt guide l'integration d'un plugin existant dans DM, quelle que soit la plateforme (LibreOffice .oxt, Thunderbird .xpi, Chrome .crx, Firefox .xpi).

---

## Etape 1 — Analyse de la base de code existante

Avant toute modification, analyser :

1. **Type de plugin** : LibreOffice (.oxt), Thunderbird (.xpi), Chrome/Firefox (.crx/.xpi)
2. **Fichier manifest** : `manifest.json` (WebExtension), `install.rdf` (legacy XUL), `description.xml` (OXT)
3. **Version actuelle** : extraite du manifest
4. **Configuration existante** : comment le plugin obtient ses parametres (hardcode, fichier local, preferences, remote config)
5. **Authentification existante** : Keycloak/OIDC, token API, credentials locales
6. **Telemetrie existante** : OpenTelemetry, logs console, rien
7. **Mecanisme de mise a jour** : auto-update, store, manuel
8. **Points d'entree** : startup/bootstrap, background script, service worker

---

## Etape 2 — Fichiers a creer

### 2.1 dm-config.json (template de configuration)

Ce fichier est embarque dans le package distribue. DM l'extrait lors de l'upload et le stocke en DB.
Il est ensuite servi aux plugins via `GET /config/{slug}/config.json?profile=<env>`.

```json
{
  "configVersion": 1,
  "default": {
    "enabled": true,
    "systemPrompt": "...",
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
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}"
  },
  "int": {
    "_description": "Integration / recette"
  },
  "prod": {
    "_description": "Production"
  }
}
```

**Regles** :
- `default` contient les valeurs communes a tous les profils
- Les sections `dev`/`int`/`prod` overrident le `default` par merge
- Les `${{VAR}}` sont des placeholders substitues par le serveur DM au runtime
- La section `local` est utilisee quand le plugin tourne sans DM
- Les sections serveur (`dev`, `int`, `prod`) sont auto-completees par DM avec les placeholders si absents

### 2.2 dm-manifest.json (metadonnees du plugin)

```json
{
  "slug": "mon-plugin",
  "name": "Mon Plugin — Description Courte",
  "description": "Description detaillee du plugin.",
  "intent": "Proposition de valeur en 1-2 phrases.",
  "device_type": "libreoffice|matisse|firefox|chrome",
  "category": "productivity|security|communication|tools",
  "publisher": "DTNUM",
  "visibility": "public",
  "homepage_url": "https://github.com/...",
  "support_email": "support@example.com",
  "icon_url": "assets/logo.png",
  "doc_url": "https://...",
  "license": "MPL-2.0",
  "key_features": [
    "Feature 1",
    "Feature 2"
  ],
  "changelog": [
    {
      "version": "1.0.0",
      "date": "2026-01-01",
      "changes": [
        "Premiere version",
        "Integration Device Management"
      ]
    }
  ]
}
```

### 2.3 Module bootstrap DM (a integrer dans le plugin)

Le plugin doit, au demarrage :

```
1. Lire le slug et la config_path depuis les preferences/storage locales
2. Appeler GET {bootstrap_url}/config/{slug}/config.json?profile={profile}
   Headers: X-Plugin-Version, X-Client-UUID, X-Platform-Type, X-User-Email (si connu)
3. Stocker la config recue localement (cache)
4. Si la reponse contient "update" != null :
   a. Comparer update.target_version avec la version actuelle
   b. Si mise a jour disponible : telecharger update.artifact_url, verifier update.checksum
   c. Installer la mise a jour (mecanisme specifique a la plateforme)
5. Si "features" est present : appliquer les feature flags
6. Si la config contient telemetryEndpoint + telemetryKey : configurer la telemetrie
```

**Headers a envoyer** :

| Header | Valeur | Obligatoire |
|--------|--------|-------------|
| `X-Plugin-Version` | Version actuelle du plugin | Oui |
| `X-Client-UUID` | UUID unique du device (genere au premier lancement, persiste) | Oui |
| `X-Platform-Type` | `libreoffice`, `thunderbird`, `chrome`, `firefox` | Recommande |
| `X-Platform-Version` | Version de l'hote (ex: `7.6.4`, `128.5.0`) | Recommande |
| `X-User-Email` | Email de l'utilisateur (si connu) | Optionnel |
| `X-Relay-Client` | Relay client ID (apres enrollment) | Pour recevoir les secrets |
| `X-Relay-Key` | Relay key (apres enrollment) | Pour recevoir les secrets |

**Reponse JSON** :

```json
{
  "meta": {
    "schema_version": 2,
    "device_type": "libreoffice",
    "device_name": "mirai-libreoffice",
    "profile": "int"
  },
  "config": {
    "enabled": true,
    "llm_base_urls": "https://api.example.com/v1",
    "llm_api_tokens": "",
    "telemetryEndpoint": "https://bootstrap.example.com/telemetry/v1/traces",
    "telemetryKey": "eyJ...",
    "device_name": "mirai-libreoffice",
    "config_path": "/config/mirai-libreoffice/config.json"
  },
  "update": {
    "action": "update",
    "current_version": "0.9.0",
    "target_version": "1.0.0",
    "artifact_url": "/catalog/mirai-libreoffice/download",
    "checksum": "sha256:abc123...",
    "urgency": "normal",
    "campaign_id": 42
  },
  "features": {
    "dark_mode": true,
    "experimental_api": false
  }
}
```

---

## Etape 3 — Enrollment (optionnel, pour recevoir les secrets)

L'enrollment permet au plugin de recevoir les secrets (tokens LLM, etc.) qui sont masques par defaut.

```
POST {bootstrap_url}/enroll
Headers:
  Authorization: Bearer {access_token_keycloak}
  Content-Type: application/json
Body:
  {
    "email": "user@example.com",
    "device_name": "{slug}",
    "client_uuid": "{client_uuid}",
    "encryption_key": "{generated_key}"
  }

Reponse:
  {
    "status": "ENROLLED",
    "relay_client_id": "rc_abc123...",
    "relay_key": "rk_xyz789..."
  }
```

Apres enrollment, le plugin doit persister `relay_client_id` et `relay_key` et les envoyer dans les headers `X-Relay-Client` / `X-Relay-Key` a chaque appel config pour recevoir les secrets (llm_api_tokens, etc.).

---

## Etape 4 — Telemetrie

Si la config contient `telemetryEnabled: true` et un `telemetryEndpoint` :

```
POST {telemetryEndpoint}
Headers:
  Authorization: Bearer {telemetryKey}
  Content-Type: application/json
  X-Client-UUID: {client_uuid}
Body: (format OTLP JSON simplifie)
  {
    "resourceSpans": [{
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "{slug}"}},
          {"key": "plugin.version", "value": {"stringValue": "1.0.0"}}
        ]
      },
      "scopeSpans": [{
        "spans": [{
          "name": "ActionName",
          "startTimeUnixNano": "...",
          "endTimeUnixNano": "...",
          "attributes": [
            {"key": "plugin.action", "value": {"stringValue": "extend|edit|summarize|..."}},
            {"key": "user.email_hash", "value": {"stringValue": "sha256:..."}}
          ]
        }]
      }]
    }]
  }
```

Le `telemetryKey` est un token court-duree (5 min). Le plugin peut le rafraichir via :
```
GET {bootstrap_url}/telemetry/token?device={slug}&profile={profile}
```

---

## Etape 5 — Deploiement via script

Creer un script `scripts/deploy-release.sh` :

```bash
#!/usr/bin/env bash
# Deploy via Device Management unified endpoint
set -euo pipefail

SLUG="mon-plugin"
BOOTSTRAP_URL="${BOOTSTRAP_URL:-https://bootstrap.example.com}"
ADMIN_TOKEN="${DM_ADMIN_TOKEN:-}"
BINARY="dist/package.{oxt|xpi|crx}"

[ -n "$ADMIN_TOKEN" ] || { echo "ERROR: DM_ADMIN_TOKEN not set"; exit 1; }

echo "Deploying $SLUG..."
RESPONSE=$(curl -s -X POST \
  "${BOOTSTRAP_URL}/api/plugins/${SLUG}/deploy" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -F "binary=@${BINARY}" \
  -F "strategy=canary")

echo "$RESPONSE" | python3 -m json.tool
```

---

## Etape 6 — Checklist d'integration

- [ ] `dm-config.json` cree avec sections default + local + dev + int + prod
- [ ] `dm-manifest.json` cree avec slug, name, description, changelog, key_features
- [ ] Icone du plugin (`assets/logo.png` ou similaire) referencee dans le manifest
- [ ] Module bootstrap : fetch config au demarrage, cache local, fallback si DM unreachable
- [ ] Client UUID genere au premier lancement et persiste
- [ ] Header `X-Plugin-Version` envoye a chaque appel config
- [ ] Gestion de la directive `update` (telechargement + installation)
- [ ] Telemetrie configuree depuis la config DM (endpoint + token)
- [ ] Script `deploy-release.sh` operationnel
- [ ] Le plugin fonctionne en mode `local` (sans DM) comme fallback
- [ ] Le binaire distribue ne contient PAS les dm-config.json/dm-manifest.json (DM les retire automatiquement)

---

## Notes par plateforme

### LibreOffice (.oxt)
- Manifest : `description.xml` (version dans `<version value="...">`)
- Build : ZIP rename en .oxt
- Update : remplacer le .oxt dans le profil utilisateur, relancer LO
- dm-config.json et dm-manifest.json a la racine du ZIP

### Thunderbird Legacy (.xpi, TB60)
- Manifest : `install.rdf` (em:version)
- Build : ZIP rename en .xpi (`dist/packagexpi.sh`)
- Update : `AddonManager.getInstallForFile()` ou `updateURL` dans install.rdf
- dm-config.json et dm-manifest.json a la racine du ZIP
- Attention : les modules ChromeUtils.import() ne supportent pas ES modules

### Thunderbird WebExtension (.xpi, TB128+)
- Manifest : `manifest.json` (version)
- Build : ZIP rename en .xpi
- Update : `browser.runtime.reload()` ou update_url dans manifest
- dm-config.json et dm-manifest.json a la racine du ZIP

### Chrome / Firefox WebExtension (.crx / .xpi)
- Manifest : `manifest.json` (version, manifest_version 3)
- Build : ZIP rename en .crx/.xpi ou via Chrome Web Store
- Update : `chrome.runtime.requestUpdateCheck()` ou external update
- dm-config.json et dm-manifest.json a la racine du ZIP
- CORS : les appels vers DM necessitent `host_permissions` dans le manifest
