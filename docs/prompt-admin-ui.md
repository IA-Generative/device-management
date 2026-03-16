# Prompt J — Admin UI : Interface d'administration Device Management

> Version : 1.1 — 2026-03-16
> Périmètre : device-management
> Stack : FastAPI + Jinja2 + HTMX + **DSFR** (Système de Design de l'État Français)
> Référence : docs/mode-operatoire-campagnes.md, docs/plugin-dm-protocol-update-features.md

---

## Contexte et objectif

Le Device Management (DM) est un backend FastAPI pur JSON (`app/main.py`). Toutes les
opérations de gestion (campagnes, cohortes, feature flags, artifacts) se font aujourd'hui
en SQL direct. L'objectif est d'ajouter une interface web d'administration complète,
accessible uniquement aux membres du groupe Keycloak **`admin-dm`**, sans build JS externe.

**Benchmark de référence** (patterns retenus) :
- LaunchDarkly : drag-to-reorder des règles, guarded rollout avec metric tiles
- Unleash : vue multi-environnement, constraint builder avec opérateurs semver
- Flagsmith : per-device override, remote config value
- Argo Rollouts : step progress indicator, boutons Pause/Abort/Promote
- Microsoft Intune : per-device tabbed detail, drill-down par setting

---

## Stack technique

```
app/
  admin/
    router.py            ← FastAPI router préfixé /admin
    auth.py              ← middleware OIDC Keycloak + vérif groupe admin-dm
    templates/           ← Jinja2
      base.html          ← layout, nav, HTMX CDN, Tailwind CDN
      dashboard.html     ← métriques système
      devices.html       ← liste des devices
      device_detail.html ← détail device (tabs)
      cohorts.html       ← liste + création cohortes
      cohort_edit.html   ← éditeur de cohorte
      feature_flags.html ← liste feature flags
      flag_detail.html   ← détail flag + overrides
      artifacts.html     ← liste + upload artifacts
      campaigns.html     ← liste campagnes
      campaign_new.html  ← wizard création (3 étapes)
      campaign_detail.html ← monitoring temps réel
      audit_log.html     ← journal des actions admin
    static/
      dsfr/              ← assets DSFR copiés localement (voir §3.0)
      dm-admin.css       ← overrides minimes spécifiques DM
```

**Dépendances à ajouter dans requirements.txt :**
```
jinja2>=3.1.0
python-multipart>=0.0.9   # upload binaires
aiofiles>=23.0.0
```

---

## 1. Authentification et autorisation

### 1.1 Flux OIDC Keycloak (Authorization Code + PKCE côté serveur)

L'admin UI utilise un **client Keycloak confidentiel** (pas le client plugin public).

Variables d'environnement à ajouter dans `.env` / `settings.py` :
```
ADMIN_OIDC_CLIENT_ID=admin-dm-ui
ADMIN_OIDC_CLIENT_SECRET=<secret>
ADMIN_OIDC_ISSUER_URL=https://keycloak.example.com/realms/mirai
ADMIN_OIDC_REDIRECT_URI=https://dm.example.com/admin/callback
ADMIN_REQUIRED_GROUP=admin-dm
ADMIN_SESSION_SECRET=<32-bytes-hex>
```

### 1.2 Implémentation `app/admin/auth.py`

```python
"""
OIDC authentication middleware for the admin UI.
- Authorization Code flow (server-side, no PKCE needed for confidential client)
- Session stored in signed cookie (itsdangerous)
- Group membership check: token must contain `admin-dm` in
  `resource_access.admin-dm-ui.roles` OR `groups` claim
"""

import hashlib, hmac, json, os, time, urllib.parse, urllib.request
from functools import wraps
from fastapi import Request
from fastapi.responses import RedirectResponse

SESSION_COOKIE = "dm_admin_session"
SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "changeme")
SESSION_TTL    = 3600  # 1 heure

OIDC_ISSUER       = os.getenv("ADMIN_OIDC_ISSUER_URL", "")
CLIENT_ID         = os.getenv("ADMIN_OIDC_CLIENT_ID", "admin-dm-ui")
CLIENT_SECRET     = os.getenv("ADMIN_OIDC_CLIENT_SECRET", "")
REDIRECT_URI      = os.getenv("ADMIN_OIDC_REDIRECT_URI", "")
REQUIRED_GROUP    = os.getenv("ADMIN_REQUIRED_GROUP", "admin-dm")

# Résolution OIDC discovery
_oidc_config = {}

def _get_oidc_config():
    global _oidc_config
    if _oidc_config:
        return _oidc_config
    url = OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=5) as r:
        _oidc_config = json.loads(r.read())
    return _oidc_config

def _sign_session(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    import base64
    return base64.urlsafe_b64encode(f"{sig}.{payload}".encode()).decode()

def _verify_session(cookie: str) -> dict | None:
    try:
        import base64
        raw = base64.urlsafe_b64decode(cookie.encode()).decode()
        sig, payload = raw.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if time.time() > data.get("exp", 0):
            return None
        return data
    except Exception:
        return None

def _has_admin_group(token_claims: dict) -> bool:
    groups = token_claims.get("groups", [])
    if REQUIRED_GROUP in groups:
        return True
    roles = (token_claims
             .get("resource_access", {})
             .get(CLIENT_ID, {})
             .get("roles", []))
    return REQUIRED_GROUP in roles

def require_admin(func):
    """Decorator: redirect to OIDC login if session is missing/invalid/unauthorized."""
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        cookie = request.cookies.get(SESSION_COOKIE)
        session = _verify_session(cookie) if cookie else None
        if not session:
            state = os.urandom(16).hex()
            cfg = _get_oidc_config()
            params = urllib.parse.urlencode({
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": "openid profile email groups",
                "state": state,
            })
            resp = RedirectResponse(f"{cfg['authorization_endpoint']}?{params}")
            resp.set_cookie("dm_oidc_state", state, httponly=True, samesite="lax")
            return resp
        request.state.admin_session = session
        return await func(request, *args, **kwargs)
    return wrapper
```

### 1.3 Routes OIDC dans `router.py`

```python
@router.get("/callback")
async def oidc_callback(request: Request, code: str, state: str):
    """Exchange authorization code for tokens, verify group, set session cookie."""
    stored_state = request.cookies.get("dm_oidc_state")
    if state != stored_state:
        raise HTTPException(400, "Invalid state")

    cfg = _get_oidc_config()
    # Exchange code → tokens
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(cfg["token_endpoint"], data=data,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        tokens = json.loads(r.read())

    # Decode id_token (no signature verification needed here — already via HTTPS)
    import base64
    payload_b64 = tokens["id_token"].split(".")[1] + "=="
    claims = json.loads(base64.urlsafe_b64decode(payload_b64))

    if not _has_admin_group(claims):
        raise HTTPException(403, "Accès refusé : groupe admin-dm requis")

    session = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name", claims.get("preferred_username")),
        "exp": int(time.time()) + SESSION_TTL,
    }
    resp = RedirectResponse("/admin/")
    resp.set_cookie(SESSION_COOKIE, _sign_session(session),
                    httponly=True, samesite="lax", max_age=SESSION_TTL)
    resp.delete_cookie("dm_oidc_state")
    return resp

@router.get("/logout")
async def logout():
    resp = RedirectResponse("/admin/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp
```

### 1.4 NFR Sécurité

| Exigence | Implémentation |
|---|---|
| Accès admin réservé au groupe `admin-dm` | Vérifié sur chaque requête via `@require_admin` |
| Session signée HMAC-SHA256 | Cookie `HttpOnly; SameSite=Lax` |
| TTL session 1h | Champ `exp` dans le payload signé |
| Toute action admin loguée | Table `admin_audit_log` + log structuré |
| Pas de secret client dans le navigateur | Flux server-side Authorization Code |
| CORS : pas de credentiels cross-origin | CORSMiddleware `allow_credentials=False` (déjà présent) |
| Upload binaires : taille max 100 Mo | `Content-Length` check avant écriture |
| Upload binaires : extension autorisée | Whitelist `.oxt`, `.xpi`, `.crx` |
| Injection SQL | Requêtes paramétrées psycopg2 (jamais de f-string SQL) |
| XSS | Jinja2 auto-escape activé (défaut) |

---

## 2. Table d'audit

### 2.1 Migration DB `003_admin_audit.sql`

```sql
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_email  TEXT NOT NULL,
    actor_sub    TEXT NOT NULL,
    action       TEXT NOT NULL,        -- ex: "campaign.activate", "flag.update"
    resource_type TEXT NOT NULL,       -- "campaign" | "cohort" | "flag" | "artifact" | "device"
    resource_id  TEXT,                 -- id ou slug de la ressource
    payload      JSONB,                -- diff avant/après
    ip_address   INET,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON admin_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON admin_audit_log (actor_email);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON admin_audit_log (resource_type, resource_id);
```

### 2.2 Helper d'audit

```python
def audit_log(cur, *, actor: dict, action: str, resource_type: str,
              resource_id: str = None, payload: dict = None,
              ip: str = None, ua: str = None):
    cur.execute("""
        INSERT INTO admin_audit_log
          (actor_email, actor_sub, action, resource_type, resource_id, payload, ip_address, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s::inet, %s)
    """, (
        actor.get("email"), actor.get("sub"),
        action, resource_type, resource_id,
        json.dumps(payload) if payload else None,
        ip, ua
    ))
```

**Chaque POST/PUT/DELETE admin doit appeler `audit_log()` dans la même transaction.**

---

## 3. Écrans à implémenter

### 3.0 Système de design — DSFR

L'interface **doit respecter le Système de Design de l'État Français (DSFR)**.
Référence officielle : https://www.systeme-de-design.gouv.fr/

#### Installation des assets DSFR (sans build JS)

```bash
# Télécharger le package DSFR depuis la release officielle GitHub
# https://github.com/GouvernementFR/dsfr/releases (prendre la dernière version)
# Exemple avec v1.13.0 :
wget https://github.com/GouvernementFR/dsfr/releases/download/v1.13.0/dsfr-v1.13.0.zip
unzip dsfr-v1.13.0.zip -d app/admin/static/dsfr/
# On garde uniquement : dsfr.min.css, dsfr.module.min.js, icons/ et fonts/
```

Ajouter une route de service des statics dans `main.py` :
```python
from fastapi.staticfiles import StaticFiles
app.mount("/admin/static", StaticFiles(directory="app/admin/static"), name="admin-static")
```

#### Palette DSFR utilisée

Le plugin LibreOffice utilise déjà les couleurs DSFR (`_UI` dict dans `entrypoint.py`).
Appliquer le même référentiel :

| Token DSFR | Valeur | Usage |
|---|---|---|
| `--blue-france-sun-113` | `#000091` | Fond header, boutons primaires |
| `--blue-france-925` | `#F5F5FE` | Fond pages, zones accent |
| `--grey-950` | `#F6F6F6` | Fond sections, sidebar |
| `--text-title-grey` | `#161616` | Texte principal |
| `--text-mention-grey` | `#666666` | Texte secondaire |
| `--success-425` | `#18753C` | Badge OK, succès |
| `--warning-425` | `#B34000` | Badge Inactif, avertissement |
| `--error-425` | `#CE0500` | Badge Erreur, danger |
| `--info-425` | `#0063CB` | Badge Info |

#### Composants DSFR à utiliser (mapping UI → composant)

| Élément UI | Composant DSFR | Classe |
|---|---|---|
| Navigation principale | Header + Navigation | `fr-header`, `fr-nav` |
| Bouton primaire | Bouton | `fr-btn` |
| Bouton danger | Bouton | `fr-btn fr-btn--secondary fr-btn--icon-left fr-icon-close-circle-line` |
| Tableau de données | Tableau | `fr-table` |
| Onglets | Onglets | `fr-tabs`, `fr-tabs__tab`, `fr-tabs__panel` |
| Formulaire | Groupe de champ | `fr-input-group`, `fr-label`, `fr-input` |
| Menu déroulant | Select | `fr-select-group`, `fr-select` |
| Alerte succès | Alerte | `fr-alert fr-alert--success` |
| Alerte erreur | Alerte | `fr-alert fr-alert--error` |
| Alerte avertissement | Alerte | `fr-alert fr-alert--warning` |
| Badge statut | Badge | `fr-badge`, `fr-badge--success`, `fr-badge--warning`, `fr-badge--error` |
| Carte info | Carte | `fr-card` |
| Barre de progression | (CSS custom) | Classe `dm-progress-bar` dans `dm-admin.css` |
| Modal confirmation | Modale | `fr-modal` |
| Fil d'Ariane | Fil d'Ariane | `fr-breadcrumb` |
| Pagination | Pagination | `fr-pagination` |
| Champ de recherche | Barre de recherche | `fr-search-bar` |
| Upload fichier | Champ fichier | `fr-upload-group` |

#### `dm-admin.css` — composants spécifiques DM

```css
/* Barre de progression campagne */
.dm-progress-bar {
  background-color: var(--grey-925-125);
  border-radius: 4px;
  height: 8px;
  overflow: hidden;
}
.dm-progress-bar__fill {
  height: 100%;
  background-color: var(--blue-france-sun-113);
  transition: width 0.4s ease;
}
.dm-progress-bar__fill--error {
  background-color: var(--error-425);
}

/* Metric tile */
.dm-metric-tile {
  background: var(--blue-france-925);
  border-left: 4px solid var(--blue-france-sun-113);
  padding: 1rem 1.5rem;
  border-radius: 4px;
}
.dm-metric-tile--warning { border-left-color: var(--warning-425); }
.dm-metric-tile--error   { border-left-color: var(--error-425); }
.dm-metric-tile--success { border-left-color: var(--success-425); }

/* Step indicator (campagne) */
.dm-steps { display: flex; align-items: center; gap: 0; }
.dm-step {
  padding: 0.5rem 1.25rem;
  background: var(--grey-950);
  border: 1px solid var(--border-default-grey);
  font-size: 0.875rem;
  color: var(--text-mention-grey);
}
.dm-step--done   { background: var(--success-425); color: #fff; }
.dm-step--active { background: var(--blue-france-sun-113); color: #fff; font-weight: bold; }
.dm-step--arrow::after { content: '›'; margin-left: 0.75rem; color: var(--grey-625); }

/* Badge santé device */
.dm-health-ok     { background: var(--success-425); color: #fff; }
.dm-health-stale  { background: var(--warning-425); color: #fff; }
.dm-health-error  { background: var(--error-425); color: #fff; }
.dm-health-never  { background: var(--grey-625); color: #fff; }
```

---

### 3.1 Layout global `base.html`

```html
<!DOCTYPE html>
<html lang="fr" data-fr-scheme="system">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}DM Admin{% endblock %} — Administration Device Management</title>
  <!-- DSFR -->
  <link rel="stylesheet" href="/admin/static/dsfr/dsfr.min.css">
  <!-- Overrides DM -->
  <link rel="stylesheet" href="/admin/static/dm-admin.css">
  <!-- HTMX (pas de build requis) -->
  <script src="https://unpkg.com/htmx.org@2.0.3" defer></script>
</head>
<body>

<!-- ── En-tête DSFR ─────────────────────────────────────── -->
<header role="banner" class="fr-header">
  <div class="fr-header__body">
    <div class="fr-container">
      <div class="fr-header__body-row">
        <div class="fr-header__brand fr-enlarge-link">
          <div class="fr-header__brand-top">
            <div class="fr-header__logo">
              <p class="fr-logo">République<br>Française</p>
            </div>
            <div class="fr-header__navbar">
              <button class="fr-btn--menu fr-btn" data-fr-opened="false"
                      aria-controls="modal-header__menu" aria-haspopup="menu">
                Menu
              </button>
            </div>
          </div>
          <div class="fr-header__service">
            <a href="/admin/" title="Accueil — DM Admin">
              <p class="fr-header__service-title">DM Admin</p>
              <p class="fr-header__service-tagline">Administration Device Management</p>
            </a>
          </div>
        </div>
        <div class="fr-header__tools">
          <div class="fr-header__tools-links">
            <ul class="fr-btns-group">
              <li>
                <span class="fr-text--sm fr-text-color--grey">
                  {{ request.state.admin_session.name }}
                </span>
              </li>
              <li>
                <a class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline fr-icon-logout-box-r-line"
                   href="/admin/logout">
                  Déconnexion
                </a>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Navigation principale -->
  <div class="fr-header__menu fr-modal" id="modal-header__menu" aria-labelledby="button-menu">
    <div class="fr-container">
      <button class="fr-link--close fr-link" aria-controls="modal-header__menu">Fermer</button>
      <div class="fr-header__menu-links"></div>
      <nav class="fr-nav" id="navigation-775" role="navigation" aria-label="Menu principal">
        <ul class="fr-nav__list">
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if request.url.path == '/admin/' %}fr-nav__link--active{% endif %}"
               href="/admin/" aria-current="{% if request.url.path == '/admin/' %}page{% endif %}">
              Tableau de bord
            </a>
          </li>
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if '/admin/devices' in request.url.path %}fr-nav__link--active{% endif %}"
               href="/admin/devices">Appareils</a>
          </li>
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if '/admin/cohorts' in request.url.path %}fr-nav__link--active{% endif %}"
               href="/admin/cohorts">Cohortes</a>
          </li>
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if '/admin/flags' in request.url.path %}fr-nav__link--active{% endif %}"
               href="/admin/flags">Feature flags</a>
          </li>
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if '/admin/artifacts' in request.url.path %}fr-nav__link--active{% endif %}"
               href="/admin/artifacts">Artifacts</a>
          </li>
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if '/admin/campaigns' in request.url.path %}fr-nav__link--active{% endif %}"
               href="/admin/campaigns">Campagnes</a>
          </li>
          <li class="fr-nav__item">
            <a class="fr-nav__link {% if '/admin/audit' in request.url.path %}fr-nav__link--active{% endif %}"
               href="/admin/audit">Journal d'audit</a>
          </li>
        </ul>
      </nav>
    </div>
  </div>
</header>

<!-- ── Alertes flash ─────────────────────────────────────── -->
{% if flash_message %}
<div class="fr-container fr-mt-2w">
  <div class="fr-alert fr-alert--{{ flash_type | default('success') }} fr-alert--sm">
    <p>{{ flash_message }}</p>
  </div>
</div>
{% endif %}

<!-- ── Contenu principal ─────────────────────────────────── -->
<main role="main" id="content" class="fr-container fr-my-4w">
  {% block breadcrumb %}{% endblock %}
  {% block content %}{% endblock %}
</main>

<!-- ── Pied de page DSFR ─────────────────────────────────── -->
<footer class="fr-footer" role="contentinfo">
  <div class="fr-container">
    <div class="fr-footer__body">
      <div class="fr-footer__brand fr-enlarge-link">
        <p class="fr-logo">République<br>Française</p>
      </div>
      <div class="fr-footer__content">
        <p class="fr-footer__content-desc">
          Device Management — Interface d'administration réservée aux membres du groupe admin-dm.
        </p>
      </div>
    </div>
  </div>
</footer>

<script type="module" src="/admin/static/dsfr/dsfr.module.min.js"></script>
</body>
</html>
```

#### Exemples de composants DSFR dans les templates

**Boutons d'action campagne :**
```html
<!-- Activer -->
<button class="fr-btn fr-btn--icon-left fr-icon-play-circle-line"
        hx-post="/admin/campaigns/{{id}}/activate" hx-confirm="Activer cette campagne ?">
  Activer
</button>
<!-- Pause -->
<button class="fr-btn fr-btn--secondary fr-btn--icon-left fr-icon-pause-circle-line"
        hx-post="/admin/campaigns/{{id}}/pause">
  Pause
</button>
<!-- Rollback -->
<button class="fr-btn fr-btn--tertiary fr-btn--icon-left fr-icon-arrow-go-back-line"
        data-fr-opened="false" aria-controls="modal-rollback-{{id}}">
  Rollback
</button>
```

**Badge statut campagne :**
```html
{% if campaign.status == 'active' %}
  <p class="fr-badge fr-badge--success fr-badge--no-icon">Actif</p>
{% elif campaign.status == 'draft' %}
  <p class="fr-badge fr-badge--info fr-badge--no-icon">Brouillon</p>
{% elif campaign.status == 'paused' %}
  <p class="fr-badge fr-badge--warning fr-badge--no-icon">En pause</p>
{% elif campaign.status == 'rolled_back' %}
  <p class="fr-badge fr-badge--error fr-badge--no-icon">Rollback</p>
{% elif campaign.status == 'completed' %}
  <p class="fr-badge fr-badge--new fr-badge--no-icon">Terminé</p>
{% endif %}
```

**Badge santé device :**
```html
<span class="fr-badge fr-badge--sm fr-badge--no-icon dm-health-{{ device.health }}">
  {% if device.health == 'ok' %}🟢 OK
  {% elif device.health == 'stale' %}🟡 Inactif
  {% elif device.health == 'error' %}🔴 Erreur
  {% else %}⚫ Jamais vu{% endif %}
</span>
```

**Barre de recherche :**
```html
<div class="fr-search-bar" id="search-owner-bar" role="search">
  <label class="fr-label" for="search-owner">Rechercher un appareil</label>
  <input class="fr-input" type="search" id="search-owner" name="owner"
         placeholder="Email ou nom du propriétaire…"
         hx-get="/admin/devices" hx-target="#device-table" hx-swap="outerHTML"
         hx-trigger="keyup changed delay:300ms">
  <button class="fr-btn" title="Rechercher">Rechercher</button>
</div>
```

**Tableau de données :**
```html
<div class="fr-table fr-table--bordered" id="device-table">
  <table>
    <caption>Liste des appareils</caption>
    <thead>
      <tr>
        <th scope="col">Propriétaire</th>
        <th scope="col">Plateforme</th>
        <th scope="col">Version</th>
        <th scope="col">État</th>
        <th scope="col">Dernière action</th>
        <th scope="col">Connexion</th>
        <th scope="col">Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for d in devices %}
      <tr>
        <td>{{ d.email }}</td>
        <td>{{ d.platform_type }}</td>
        <td><code>{{ d.plugin_version }}</code></td>
        <td><!-- badge santé --></td>
        <td>{{ d.last_action | span_label }}</td>
        <td>{{ d.last_contact | timeago }}</td>
        <td>
          <a class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
             href="/admin/devices/{{ d.client_uuid }}">Détail</a>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
```

**Onglets détail device :**
```html
<div class="fr-tabs">
  <ul class="fr-tabs__list" role="tablist" aria-label="Détail appareil">
    <li role="presentation">
      <button class="fr-tabs__tab" tabindex="0" role="tab" aria-selected="true"
              aria-controls="tab-infos">Infos & Santé</button>
    </li>
    <li role="presentation">
      <button class="fr-tabs__tab" tabindex="-1" role="tab"
              aria-controls="tab-campaigns"
              hx-get="/admin/api/devices/{{uuid}}/campaigns"
              hx-target="#tab-campaigns" hx-trigger="click once">
        Campagnes
      </button>
    </li>
    <li role="presentation">
      <button class="fr-tabs__tab" tabindex="-1" role="tab"
              aria-controls="tab-flags"
              hx-get="/admin/api/devices/{{uuid}}/flags"
              hx-target="#tab-flags" hx-trigger="click once">
        Feature flags
      </button>
    </li>
    <li role="presentation">
      <button class="fr-tabs__tab" tabindex="-1" role="tab"
              aria-controls="tab-history">Historique</button>
    </li>
    <li role="presentation">
      <button class="fr-tabs__tab" tabindex="-1" role="tab"
              aria-controls="tab-activity"
              hx-get="/admin/api/devices/{{uuid}}/activity"
              hx-target="#tab-activity" hx-trigger="click once">
        Activité récente
      </button>
    </li>
  </ul>
  <div id="tab-infos"     class="fr-tabs__panel fr-tabs__panel--selected" role="tabpanel">...</div>
  <div id="tab-campaigns" class="fr-tabs__panel" role="tabpanel"><p class="fr-text--sm">Chargement…</p></div>
  <div id="tab-flags"     class="fr-tabs__panel" role="tabpanel"><p class="fr-text--sm">Chargement…</p></div>
  <div id="tab-history"   class="fr-tabs__panel" role="tabpanel">...</div>
  <div id="tab-activity"  class="fr-tabs__panel" role="tabpanel"><p class="fr-text--sm">Chargement…</p></div>
</div>
```

**Modale de confirmation rollback :**
```html
<dialog id="modal-rollback-{{id}}" class="fr-modal" role="dialog"
        aria-labelledby="modal-rollback-title-{{id}}">
  <div class="fr-container fr-container--fluid fr-container-md">
    <div class="fr-grid-row fr-grid-row--center">
      <div class="fr-col-12 fr-col-md-8 fr-col-lg-6">
        <div class="fr-modal__body">
          <div class="fr-modal__header">
            <button class="fr-btn--close fr-btn" aria-controls="modal-rollback-{{id}}">
              Fermer
            </button>
          </div>
          <div class="fr-modal__content">
            <h1 class="fr-modal__title" id="modal-rollback-title-{{id}}">
              <span class="fr-icon-warning-fill fr-icon--lg" aria-hidden="true"></span>
              Confirmer le rollback
            </h1>
            <div class="fr-alert fr-alert--warning fr-mb-2w">
              <p>Cette action va basculer <strong>{{ affected_count }} appareils</strong>
                 vers la version {{ rollback_version }}.</p>
            </div>
            <div class="fr-select-group">
              <label class="fr-label" for="rollback-reason">Raison <span class="fr-hint-text">Obligatoire</span></label>
              <select class="fr-select" id="rollback-reason" name="reason" required>
                <option value="">Sélectionner…</option>
                <option>Taux d'erreur dépassé</option>
                <option>Régression détectée</option>
                <option>Signalement utilisateur</option>
                <option>Test / maintenance</option>
                <option>Autre</option>
              </select>
            </div>
            <div class="fr-input-group fr-mt-1w">
              <label class="fr-label" for="rollback-comment">Commentaire libre</label>
              <textarea class="fr-input" id="rollback-comment" name="comment" rows="2"></textarea>
            </div>
          </div>
          <div class="fr-modal__footer">
            <div class="fr-btns-group fr-btns-group--right fr-btns-group--inline-reverse">
              <button class="fr-btn fr-btn--error"
                      hx-post="/admin/campaigns/{{id}}/rollback"
                      hx-include="#rollback-reason, #rollback-comment"
                      hx-on::after-request="dsfr(document.getElementById('modal-rollback-{{id}}')).modal.conceal()">
                Confirmer le rollback
              </button>
              <button class="fr-btn fr-btn--secondary"
                      aria-controls="modal-rollback-{{id}}">
                Annuler
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</dialog>
```

#### NFR DSFR obligatoires

| Exigence | Détail |
|---|---|
| Police Marianne | Incluse dans le package DSFR (`fonts/`) — ne pas utiliser d'autre police |
| Contraste AA | Vérifier avec l'outil DSFR ou axe DevTools — ratio min 4.5:1 pour le texte |
| Navigation clavier | Tous les éléments interactifs atteignables au clavier (tab order logique) |
| Attributs ARIA | `role`, `aria-label`, `aria-controls`, `aria-current` sur tous les composants DSFR |
| Pas de Tailwind | Ne pas utiliser Tailwind — utiliser uniquement les classes DSFR + `dm-admin.css` |
| Langue HTML | `<html lang="fr">` obligatoire |
| Mode sombre | `data-fr-scheme="system"` sur `<html>` — le DSFR gère automatiquement |
| Favicon | Utiliser le favicon Marianne fourni dans le package DSFR |
| Responsive | Le DSFR est responsive par défaut — ne pas casser la grille `fr-grid-row` |

---

### 3.2 Écran 1 — Tableau de bord `GET /admin/`

**Objectif** : vue d'ensemble santé du système.

**Sections** :

**Métriques système (4 tiles)** — rechargées toutes les 30s via HTMX polling :
```html
<div hx-get="/admin/api/metrics" hx-trigger="every 30s" hx-swap="outerHTML">
  <!-- 4 tiles : devices actifs 7j / taux enrôlement / taux erreur enrôlement / campagnes actives -->
</div>
```

**Requêtes SQL pour les tiles** :
```sql
-- Devices actifs (ont contacté le DM dans les 7 derniers jours)
SELECT COUNT(DISTINCT client_uuid) FROM device_connections
WHERE created_at > NOW() - INTERVAL '7 days';

-- Taux d'enrôlement (enrollés / total devices vus)
SELECT
  COUNT(*) FILTER (WHERE enrolled_at IS NOT NULL)::numeric
  / NULLIF(COUNT(*), 0) * 100
FROM device_connections;

-- Taux d'erreur enrôlement (7 derniers jours)
SELECT
  COUNT(*) FILTER (WHERE status = 'error')::numeric
  / NULLIF(COUNT(*), 0) * 100
FROM enrollment_attempts
WHERE created_at > NOW() - INTERVAL '7 days';

-- Campagnes actives
SELECT COUNT(*) FROM campaigns WHERE status = 'active';
```

**Graphiques** (Chart.js CDN) :
- Connexions DM par jour sur 30 jours (courbe)
- Taux d'erreur enrôlement par jour sur 14 jours (histogramme)

**Campagnes actives** : tableau avec colonne de progression et bouton d'action rapide.

---

### 3.3 Écran 2 — Appareils `GET /admin/devices`

#### Barre de recherche par propriétaire

La recherche par propriétaire est le point d'entrée principal. Elle doit être
**instantanée** (HTMX `hx-trigger="keyup changed delay:300ms"`) et chercher sur
l'email ET le nom affiché (claim Keycloak `name` / `preferred_username`).

```html
<form hx-get="/admin/devices" hx-target="#device-table" hx-swap="outerHTML"
      hx-trigger="keyup changed delay:300ms from:#search-owner, change from:select">
  <input id="search-owner" name="owner" placeholder="Rechercher par email ou nom…"
         class="w-80 border rounded px-3 py-2" autofocus>
  <select name="platform">
    <option value="">Toutes les plateformes</option>
    <option value="libreoffice">LibreOffice</option>
    <option value="thunderbird">Thunderbird</option>
    <option value="chrome">Chrome/Edge</option>
  </select>
  <select name="health">
    <option value="">Tous les états</option>
    <option value="ok">🟢 OK</option>
    <option value="stale">🟡 Inactif (> 24h)</option>
    <option value="error">🔴 En erreur</option>
    <option value="never">⚫ Jamais vu</option>
  </select>
  <select name="enrollment">
    <option value="">Tous</option>
    <option value="enrolled">Enrôlé</option>
    <option value="not_enrolled">Non enrôlé</option>
    <option value="error">Échec enrôlement</option>
  </select>
</form>
```

**Logique de l'état de fonctionnement (`health`)** :

```python
def compute_device_health(last_contact_at, enrollment_status, last_error):
    """
    Calcule l'état opérationnel d'un device.
    Retourne : "ok" | "stale" | "error" | "never"
    """
    if last_contact_at is None:
        return "never"   # device enrôlé mais n'a jamais contacté le DM
    delta = datetime.now(timezone.utc) - last_contact_at
    if last_error:
        return "error"   # dernière connexion s'est terminée en erreur
    if delta.total_seconds() > 86400:
        return "stale"   # inactif depuis plus de 24h
    return "ok"
```

**Badges visuels** :

| Statut | Badge | Condition |
|---|---|---|
| 🟢 OK | vert | Contact < 24h, pas d'erreur |
| 🟡 Inactif | orange | Dernier contact entre 24h et 7j |
| 🔴 En erreur | rouge | Dernière connexion en erreur ou enrôlement échoué |
| ⚫ Jamais vu | gris | Enrôlé mais aucune connexion DM enregistrée |

**Liste paginée** (50 par page) — colonnes :

| Propriétaire | Plateforme | Version plugin | Dernière connexion | État | Enrôlement | Actions |
|---|---|---|---|---|---|---|
| alice@ex.com | LibreOffice | 2.0.0 | il y a 5 min | 🟢 OK | ✓ | Détail |
| bob@ex.com | Thunderbird | 0.7.0 | il y a 2j | 🟡 Inactif | ✓ | Détail |
| carol@ex.com | LibreOffice | 1.9.0 | il y a 1h | 🔴 Erreur | ✗ | Détail |

**Requête SQL** (recherche par email + nom, filtre état, pagination) :

```sql
SELECT
    dc.client_uuid,
    dc.email,
    dc.platform_type,
    dc.plugin_version,
    MAX(dc.created_at)                                  AS last_contact,
    MAX(dc.created_at) FILTER (WHERE dc.error IS NOT NULL) AS last_error_at,
    e.status                                            AS enrollment_status,
    CASE
        WHEN MAX(dc.created_at) IS NULL                        THEN 'never'
        WHEN MAX(dc.created_at) FILTER (WHERE dc.error IS NOT NULL)
             = MAX(dc.created_at)                              THEN 'error'
        WHEN NOW() - MAX(dc.created_at) > INTERVAL '24 hours' THEN 'stale'
        ELSE 'ok'
    END                                                 AS health
FROM device_connections dc
LEFT JOIN enrollments e ON e.client_uuid = dc.client_uuid
WHERE
    (:owner IS NULL OR dc.email ILIKE '%' || :owner || '%'
                    OR dc.display_name ILIKE '%' || :owner || '%')
    AND (:platform IS NULL OR dc.platform_type = :platform)
    AND (:enrollment IS NULL OR e.status = :enrollment)
GROUP BY dc.client_uuid, dc.email, dc.platform_type, dc.plugin_version,
         dc.display_name, e.status
HAVING (:health IS NULL OR
        CASE
            WHEN MAX(dc.created_at) IS NULL THEN 'never'
            WHEN MAX(dc.created_at) FILTER (WHERE dc.error IS NOT NULL)
                 = MAX(dc.created_at) THEN 'error'
            WHEN NOW() - MAX(dc.created_at) > INTERVAL '24 hours' THEN 'stale'
            ELSE 'ok'
        END = :health)
ORDER BY
    CASE WHEN :owner IS NOT NULL AND dc.email ILIKE :owner || '%' THEN 0 ELSE 1 END,
    last_contact DESC NULLS LAST
LIMIT 50 OFFSET :offset;
```

**Raccourci clavier** : `Ctrl+K` / `Cmd+K` ouvre la barre de recherche depuis n'importe quel écran (pattern LaunchDarkly).

#### État de fonctionnement — indicateur visuel dans la liste

Chaque ligne affiche :
- Le **badge santé** (couleur + libellé)
- Le **délai depuis la dernière connexion** en langage naturel ("il y a 5 min", "il y a 2 jours")
- Une **icône d'alerte** si la version du plugin est inférieure à la version cible de la campagne active

#### Mini-dashboard santé de la flotte (haut de page)

```html
<!-- 4 compteurs rapides avec filtre cliquable -->
<div class="grid grid-cols-4 gap-4 mb-6"
     hx-get="/admin/api/devices/health-summary" hx-trigger="load, every 60s">
  <div class="cursor-pointer" hx-get="/admin/devices?health=ok">
    <span class="text-2xl font-bold text-green-600">{{ ok_count }}</span>
    <p class="text-sm text-gray-500">🟢 OK</p>
  </div>
  <div class="cursor-pointer" hx-get="/admin/devices?health=stale">
    <span class="text-2xl font-bold text-yellow-600">{{ stale_count }}</span>
    <p class="text-sm text-gray-500">🟡 Inactifs</p>
  </div>
  <div class="cursor-pointer" hx-get="/admin/devices?health=error">
    <span class="text-2xl font-bold text-red-600">{{ error_count }}</span>
    <p class="text-sm text-gray-500">🔴 En erreur</p>
  </div>
  <div class="cursor-pointer" hx-get="/admin/devices?health=never">
    <span class="text-2xl font-bold text-gray-500">{{ never_count }}</span>
    <p class="text-sm text-gray-500">⚫ Jamais vus</p>
  </div>
</div>
```

**Cliquer sur un compteur filtre immédiatement la liste.**

---

**Page détail device** `GET /admin/devices/{client_uuid}` — **5 onglets HTMX** :

- **Infos & Santé** : UUID, email, nom propriétaire, plateforme, version plugin, LO version, dernière connexion, badge santé, adresse IP dernière connexion, user-agent
- **Campagnes** : liste des `campaign_device_status` pour ce device + statut + action force override
- **Feature flags** : valeur effective de chaque flag pour ce device (résolution complète)
- **Historique connexions** : 20 dernières connexions au DM (timestamp, IP, version plugin, statut, erreur éventuelle)
- **Diagnostics** : checklist de l'état du device

#### Onglet "Infos & Santé" — section diagnostics

```
┌─────────────────────────────────────────────────────────┐
│ Diagnostic — alice@example.com                          │
├─────────────────────────────────────────────────────────┤
│ ✅ Plugin enrôlé                                        │
│ ✅ Dernière connexion il y a 3 min                      │
│ ✅ Version plugin 2.0.0 (à jour)                        │
│ ✅ Checksum artifact vérifié                            │
│ ⚠️ Telemetry : dernier envoi il y a 2h (TTL 1h dépassé)│
│ ✅ Feature flags reçus (4 flags)                        │
└─────────────────────────────────────────────────────────┘
```

**Règles de la checklist diagnostics** :

| Indicateur | Condition ✅ | Condition ⚠️ | Condition ❌ |
|---|---|---|---|
| Plugin enrôlé | `enrollments.status = 'ok'` | — | statut `error` ou absent |
| Dernière connexion | < 24h | 24h–7j | > 7j |
| Version plugin | = version cible campagne active | = version cible-1 | < version cible-1 |
| Telemetry | dernier envoi < TTL | TTL dépassé | jamais reçu |
| Feature flags reçus | schema_version=2 vu dans les logs | — | dernier fetch sans headers v2 |

**Requête SQL historique connexions** :

```sql
SELECT created_at, ip_address, plugin_version, platform_version,
       status, error_message, headers_snapshot
FROM device_connections
WHERE client_uuid = %s
ORDER BY created_at DESC
LIMIT 20;
```

**Route API** : `GET /admin/api/devices/{uuid}/health` → JSON utilisé pour la checklist diagnostics et le badge dans la liste.

#### Onglet "Activité récente" — dernières actions via télémétrie

Le DM reçoit les traces OTEL via `POST /telemetry/v1/traces` (topic `telemetry.forward`
dans le postgres_queue). Actuellement le worker les **forwarde** vers l'upstream sans stockage
local structuré. Il faut **intercepter les spans au passage** dans le worker pour alimenter
une table locale `device_telemetry_events` (buffer circulaire, max 200 entrées/device).

##### Migration — table `device_telemetry_events` (à ajouter dans `003_admin_audit.sql`)

```sql
CREATE TABLE IF NOT EXISTS device_telemetry_events (
    id             BIGSERIAL PRIMARY KEY,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_uuid    TEXT NOT NULL,
    span_name      TEXT NOT NULL,
    span_ts        TIMESTAMPTZ,
    attributes     JSONB,
    platform       TEXT,
    plugin_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_uuid
    ON device_telemetry_events (client_uuid, received_at DESC);

-- Trigger : garder les 200 derniers événements par device
CREATE OR REPLACE FUNCTION trim_telemetry_events() RETURNS trigger AS $$
BEGIN
    DELETE FROM device_telemetry_events
    WHERE client_uuid = NEW.client_uuid
      AND id NOT IN (
          SELECT id FROM device_telemetry_events
          WHERE client_uuid = NEW.client_uuid
          ORDER BY received_at DESC LIMIT 200
      );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_trim_telemetry_events
AFTER INSERT ON device_telemetry_events
FOR EACH ROW EXECUTE FUNCTION trim_telemetry_events();
```

##### Parsing dans `_process_queue_job` (worker, best-effort après le forward)

Format OTLP JSON reçu :
```json
{
  "resourceSpans": [{
    "resource": {
      "attributes": [
        {"key": "client_uuid",    "value": {"stringValue": "abc-123"}},
        {"key": "service.name",   "value": {"stringValue": "mirai-libreoffice"}},
        {"key": "plugin.version", "value": {"stringValue": "2.0.0"}}
      ]
    },
    "scopeSpans": [{
      "spans": [{
        "name": "ExtensionUpdated",
        "startTimeUnixNano": "1710500400000000000",
        "attributes": [
          {"key": "version_after", "value": {"stringValue": "2.0.0"}},
          {"key": "campaign_id",   "value": {"stringValue": "42"}}
        ]
      }]
    }]
  }]
}
```

Ajouter **après** le forward réussi dans `_process_queue_job` :
```python
# Extraction best-effort — ne doit jamais faire échouer le job
try:
    _extract_and_store_telemetry_spans(body, client_uuid=payload.get("client_uuid"))
except Exception:
    logger.debug("telemetry span extraction failed (non-fatal)", exc_info=True)
```

Implémenter `_extract_and_store_telemetry_spans(body, client_uuid)` :
- Parser le JSON OTLP (ignorer silencieusement si protobuf ou invalide)
- Pour chaque span : extraire `span_name`, `startTimeUnixNano` → `span_ts`, attributs
- `client_uuid` depuis les resource attributes OU depuis le paramètre de la fonction
- `INSERT` en batch dans `device_telemetry_events`

##### Labels lisibles pour l'affichage

| span_name | Affiché | Icône |
|---|---|---|
| ExtensionLoaded | Démarrage plugin | 🚀 |
| ExtensionUpdated | Mise à jour | ⬆️ |
| EditSelection | Réécriture IA | ✏️ |
| ExtendSelection | Extension IA | ➕ |
| TranslateSelection | Traduction | 🌐 |
| SummarizeDocument | Résumé | 📝 |
| LoginSuccess | Connexion SSO | 🔑 |
| LoginError | Échec connexion | 🔴 |
| ConfigFetched | Config rechargée | 🔄 |
| TelemetryError | Erreur télémétrie | ⚠️ |

##### Affichage dans l'onglet "Activité récente"

```sql
SELECT span_name, span_ts, attributes, plugin_version
FROM device_telemetry_events
WHERE client_uuid = %s
ORDER BY received_at DESC
LIMIT 50;
```

```
┌─────────────────────────────────────────────────────────────────┐
│ Activité récente — alice@example.com                            │
├──────────────────────┬──────────────────┬────────────────────── ┤
│ Horodatage           │ Action           │ Détails               │
├──────────────────────┼──────────────────┼───────────────────────┤
│ 2026-03-16 09:12     │ ⬆️ Mise à jour   │ 1.9.0 → 2.0.0         │
│ 2026-03-16 09:05     │ ✏️ Réécriture IA │ Writer / 247 mots     │
│ 2026-03-16 09:00     │ 🚀 Démarrage     │ v2.0.0                │
│ 2026-03-15 17:43     │ 🔑 Connexion SSO │ alice@example.com     │
└──────────────────────┴──────────────────┴───────────────────────┘
```

**Route API** : `GET /admin/api/devices/{uuid}/activity?limit=50` → fragment HTML (refresh manuel).

##### Colonne "Dernière action" dans la liste des devices

```sql
SELECT DISTINCT ON (client_uuid)
    client_uuid, span_name AS last_action, span_ts AS last_action_ts
FROM device_telemetry_events
WHERE client_uuid = ANY(%s::text[])
ORDER BY client_uuid, received_at DESC;
```

La liste devices affiche alors :

| Propriétaire | Plateforme | Version | État | Dernière action | Dernière connexion |
|---|---|---|---|---|---|
| alice@ex.com | LibreOffice | 2.0.0 | 🟢 OK | ⬆️ Mise à jour | il y a 5 min |
| bob@ex.com | LibreOffice | 1.9.0 | 🟡 Inactif | ✏️ Réécriture IA | il y a 2j |

**Action : override feature flag sur ce device** (per-individual) :
```sql
-- Créer une cohorte "device:<uuid>" de type manual si elle n'existe pas
INSERT INTO cohorts (name, type, config)
VALUES ('device-override-<uuid>', 'manual', '{}')
ON CONFLICT DO NOTHING;

-- Ajouter le device à cette cohorte
INSERT INTO cohort_members (cohort_id, identifier_type, identifier_value)
VALUES (<cohort_id>, 'device_uuid', '<client_uuid>')
ON CONFLICT DO NOTHING;

-- Créer l'override de flag pour cette cohorte
INSERT INTO feature_flag_overrides (feature_id, cohort_id, value)
VALUES (<flag_id>, <cohort_id>, <true|false>)
ON CONFLICT (feature_id, cohort_id) DO UPDATE SET value = EXCLUDED.value;
```

---

### 3.4 Écran 3 — Cohortes `GET /admin/cohorts`

**Liste** : nom, type, nb membres, nb campagnes actives.

**Création** `POST /admin/cohorts` — formulaire inline :

```html
<form hx-post="/admin/cohorts" hx-target="#cohort-list" hx-swap="outerHTML">
  <input name="name" placeholder="Nom" required>
  <select name="type">
    <option value="manual">Manuel (liste emails/UUIDs)</option>
    <option value="percentage">Pourcentage</option>
    <option value="email_pattern">Pattern email (regex)</option>
    <option value="keycloak_group">Groupe Keycloak</option>
  </select>
  <!-- champs conditionnels via HTMX selon le type -->
  <button type="submit">Créer</button>
</form>
```

**Champs conditionnels** selon `type` (via `hx-get="/admin/cohorts/config-form?type=..."`) :
- `manual` : zone de texte — une adresse email/UUID par ligne
- `percentage` : slider 0–100 + affichage "~N devices estimés"
- `email_pattern` : input regex + bouton "Tester"
- `keycloak_group` : dropdown des groupes Keycloak (via API Keycloak admin)

**Page détail cohorte** `GET /admin/cohorts/{id}` :
- Liste des membres (paginée)
- Campagnes utilisant cette cohorte
- Feature flags overridés sur cette cohorte
- Bouton "Synchroniser depuis Keycloak" (pour type `keycloak_group`)

---

### 3.5 Écran 4 — Feature Flags `GET /admin/flags`

**Liste** :

| Nom | Valeur défaut | Nb overrides | Dernière modif |
|---|---|---|---|
| writer_assistant | 🟢 true | 0 | — |
| calc_assistant | 🔴 false | 1 (pilotes-dsi → true) | 2026-03-15 |

**Création** `POST /admin/flags` :
```
nom, description, valeur_défaut (true/false)
```

**Page détail flag** `GET /admin/flags/{id}` :

Section **Valeur globale** :
```html
<form hx-post="/admin/flags/{{flag.id}}/default" hx-swap="none">
  <select name="value">
    <option value="true" {% if flag.default_value %}selected{% endif %}>Activé pour tous</option>
    <option value="false" {% if not flag.default_value %}selected{% endif %}>Désactivé par défaut</option>
  </select>
  <button>Enregistrer</button>
</form>
```

Section **Overrides par cohorte** (drag-to-reorder pour la priorité) :

| Cohorte | Valeur | Version min plugin | Actions |
|---|---|---|---|
| pilotes-dsi | ✅ true | 2.0.0 | Modifier / Supprimer |

Bouton "+ Ajouter override" → modal :
```
Cohorte : [dropdown cohortes]
Valeur : [true / false]
Version min plugin : [optionnel, ex: 2.0.0]
```

---

### 3.6 Écran 5 — Artifacts `GET /admin/artifacts`

**Liste** :

| Plateforme | Variante | Version | Checksum | Actif | Campagnes | Actions |
|---|---|---|---|---|---|---|
| libreoffice | — | 2.0.0 | sha256:abc… | ✓ | 2 | Désactiver |
| thunderbird | tb60 | 0.7.1 | sha256:def… | ✓ | 0 | — |

**Upload** `POST /admin/artifacts/upload` :

```html
<form enctype="multipart/form-data" hx-post="/admin/artifacts/upload"
      hx-encoding="multipart/form-data" hx-target="#upload-result">
  <select name="device_type">
    <option value="libreoffice">LibreOffice (.oxt)</option>
    <option value="thunderbird">Thunderbird (.xpi)</option>
    <option value="chrome">Chrome/Edge (.crx)</option>
  </select>
  <select name="platform_variant">
    <option value="">Aucune</option>
    <option value="tb60">tb60 (TB < 78)</option>
    <option value="tb128">tb128 (TB ≥ 128)</option>
    <option value="mv2">mv2</option>
    <option value="mv3">mv3</option>
  </select>
  <input name="version" placeholder="2.0.0" required>
  <input name="changelog_url" placeholder="https://... (optionnel)">
  <input type="file" name="binary" accept=".oxt,.xpi,.crx" required>
  <button type="submit">Uploader</button>
</form>
```

**Logique d'upload** :
1. Vérifier extension `.oxt` / `.xpi` / `.crx` (whitelist)
2. Vérifier `Content-Length < 100 Mo`
3. Calculer `sha256` du binaire
4. Stocker sur S3 (via `s3_client` existant) ou disque local (`BINARIES_PATH`)
5. `INSERT INTO artifacts (...)` avec le checksum calculé
6. Audit log `artifact.upload`

**Prévisualisation adoption** : graphique Chart.js montrant combien de devices sont sur chaque version.

---

### 3.7 Écran 6 — Campagnes `GET /admin/campaigns`

**Liste avec filtres** :

| Statut | Nom | Cohorte | Progression | Taux erreur | Actions |
|---|---|---|---|---|---|
| 🟢 ACTIVE | Mirai 2.0.0 canary 5% | canary | ██░░ 48% | 0.0% | Monitorer / Pause / Rollback |
| 🟡 DRAFT | Mirai 2.0.0 DSI | pilotes-dsi | — | — | Activer / Éditer |
| ✅ DONE | Mirai 1.9.0 prod | 100% | ████ 100% | 0.1% | Voir |

**Actions rapides par ligne** :
- `DRAFT` → [Activer] [Supprimer]
- `ACTIVE` → [Pause] [Rollback] [Élargir] [Détail]
- `PAUSED` → [Reprendre] [Rollback]

---

### 3.8 Écran 7 — Création campagne (wizard 3 étapes)

`GET /admin/campaigns/new` → wizard multi-steps via HTMX.

**Étape 1 — Artifact** :
```
Sélectionner l'artifact cible  : [libreoffice / 2.0.0 ▼]  (actifs seulement)
Artifact de rollback            : [libreoffice / 1.9.0 ▼]  (version antérieure)
```

**Étape 2 — Cohorte** :
```
○ Cohorte existante  [canary (5%)          ▼]
  → Estimation : ~120 devices concernés
● Nouvelle cohorte
  Type   : [percentage ▼]
  Valeur : [  5  ] %
```

**Étape 3 — Paramètres** :
```
Nom          : [Mirai 2.0.0 — canary 5%           ]
Description  : [                                    ]
Urgency      : ● normal  ○ high  ○ critical
Deadline     : [          ] (vide = aucune)
Démarrer en  : ● draft (activer manuellement)
               ○ active (immédiatement)
```

**Récapitulatif** avant validation :
```
Artifact cible  : libreoffice / 2.0.0 (sha256:abc…)
Rollback vers   : libreoffice / 1.9.0
Cohorte cible   : canary → ~120 devices (5% de la flotte)
Urgency         : normal / sans deadline
Statut initial  : DRAFT

[← Précédent]  [Créer la campagne →]
```

---

### 3.9 Écran 8 — Monitoring campagne `GET /admin/campaigns/{id}`

Auto-refresh toutes les **15s** via HTMX.

**Header** :
```
Mirai 2.0.0 — canary 5%          [⏸ Pause] [⏫ Élargir] [🔄 Rollback]
Statut : 🟢 ACTIVE   Créée : 2026-03-16 09:00   Cohorte : canary (5%)
```

**Progression** :
```html
<div hx-get="/admin/api/campaigns/{{id}}/stats"
     hx-trigger="every 15s" hx-swap="outerHTML">
  <!-- Barre de progression + metric tiles -->
</div>
```

**Metric tiles** (pattern LaunchDarkly) :
- Tile "Mis à jour" : N/Total — barre de progression
- Tile "Taux erreur" : X.X% — vert si < 2%, orange si < 10%, rouge si ≥ 10%
- Tile "Notifiés" : N devices ont reçu la directive
- Tile "En attente" : N devices dans la cohorte pas encore contactés

**Step indicator** (pattern Argo Rollouts) :
```
[✓ 5%] ──► [✓ 25%] ──► [… 100%]
   ↑ actuel (30 min)     ↑ prochain
```

**Tableau des derniers events** (20 lignes, refresh 15s) :
| Device | Email | Action | Statut | Heure |
|---|---|---|---|---|
| uuid-xxx | alice@ex.com | update | ✓ updated | 09:12 |
| uuid-yyy | bob@ex.com | update | ⏳ notified | 09:11 |

**Actions** :

`POST /admin/campaigns/{id}/activate` — activer depuis DRAFT
`POST /admin/campaigns/{id}/pause` — passer en PAUSED
`POST /admin/campaigns/{id}/resume` — reprendre depuis PAUSED
`POST /admin/campaigns/{id}/expand` + body `{"percentage": 25}` — élargir la cohorte
`POST /admin/campaigns/{id}/complete` — clôturer normalement
`POST /admin/campaigns/{id}/rollback` — rollback d'urgence

**Rollback** — modal de confirmation :
```
⚠️ Rollback Mirai 2.0.0 — canary 5%

Cela va :
• Basculer le statut → rolled_back
• 12 devices recevront action="rollback" (retour vers 1.9.0)
• Le rollback s'applique au prochain fetch config (~5 min)

Raison : [Taux d'erreur dépassé / Regression détectée / Test / Autre]
          [                    zone de texte libre                    ]

[Annuler]  [🔴 Confirmer le rollback]
```

---

### 3.10 Écran 9 — Journal d'audit `GET /admin/audit`

| Horodatage | Acteur | Action | Ressource | Détails |
|---|---|---|---|---|
| 2026-03-16 09:15 | alice@ex.com | campaign.rollback | campaign:42 | Raison: "taux erreur 15%" |
| 2026-03-16 09:00 | alice@ex.com | campaign.activate | campaign:42 | — |

Filtres : acteur, type d'action, ressource, plage de dates.
Export CSV : `GET /admin/audit/export?from=...&to=...`

---

## 4. Routes API interne (HTMX fragments)

```python
# Métriques dashboard
GET  /admin/api/metrics                     → fragment HTML tiles

# Campagne monitoring
GET  /admin/api/campaigns/{id}/stats        → fragment HTML barre + tiles

# Cohort device count estimation
GET  /admin/api/cohorts/estimate?type=percentage&value=5  → JSON {count: 120}

# Flag effective value pour un device
GET  /admin/api/devices/{uuid}/flags        → fragment HTML table flags

# Feature flags list (pour les dropdowns)
GET  /admin/api/flags                       → JSON list

# Artifacts list (pour les dropdowns)
GET  /admin/api/artifacts                   → JSON list
```

---

## 5. Tests headless à implémenter

**Fichier** : `tests/test_admin_ui.py`

```python
"""
Tests de l'admin UI — pas de Keycloak réel.
On mocke la session admin via un cookie signé forgé.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# Forger une session admin valide pour les tests
def forge_admin_cookie():
    from app.admin.auth import _sign_session
    import time
    return _sign_session({
        "sub": "test-sub",
        "email": "admin@test.com",
        "name": "Test Admin",
        "exp": int(time.time()) + 3600,
    })

@pytest.fixture
def admin_client(mock_db):
    from app.main import app
    client = TestClient(app)
    cookie = forge_admin_cookie()
    client.cookies.set("dm_admin_session", cookie)
    return client
```

**Cas de tests** :

| ID | Titre | Niveau |
|---|---|---|
| TC-ADM-01 | GET /admin/ sans session → redirect login | U |
| TC-ADM-02 | GET /admin/ avec session valide → 200 | U |
| TC-ADM-03 | Session expirée → redirect login | U |
| TC-ADM-04 | Token sans groupe admin-dm → 403 | U |
| TC-ADM-05 | GET /admin/campaigns → liste campagnes | I |
| TC-ADM-06 | POST /admin/campaigns/{id}/activate → status=active en DB | I |
| TC-ADM-07 | POST /admin/campaigns/{id}/rollback → status=rolled_back, audit_log créé | I |
| TC-ADM-08 | POST /admin/artifacts/upload fichier .exe → 400 | I |
| TC-ADM-09 | POST /admin/artifacts/upload .oxt valide → artifact en DB | I |
| TC-ADM-10 | POST /admin/flags/{id}/default → valeur mise à jour + audit | I |
| TC-ADM-11 | GET /admin/devices/{uuid} → page détail avec 5 tabs | I |
| TC-ADM-12 | POST override flag sur device individuel → cohort créée | I |
| TC-ADM-16 | Recherche par email partiel → résultats filtrés | I |
| TC-ADM-17 | Recherche par nom propriétaire → résultats filtrés | I |
| TC-ADM-18 | Filtre health=error → uniquement devices en erreur | I |
| TC-ADM-19 | Filtre health=stale → devices sans contact > 24h | I |
| TC-ADM-20 | compute_device_health : last_contact=None → "never" | U |
| TC-ADM-21 | compute_device_health : last_error → "error" | U |
| TC-ADM-22 | compute_device_health : > 24h sans erreur → "stale" | U |
| TC-ADM-23 | compute_device_health : < 24h sans erreur → "ok" | U |
| TC-ADM-24 | GET /admin/api/devices/health-summary → 4 compteurs | I |
| TC-ADM-25 | GET /admin/api/devices/{uuid}/health → checklist JSON | I |
| TC-ADM-26 | _extract_and_store_telemetry_spans : JSON OTLP valide → rows en DB | U |
| TC-ADM-27 | _extract_and_store_telemetry_spans : protobuf/invalide → pas d'erreur | U |
| TC-ADM-28 | _extract_and_store_telemetry_spans : client_uuid manquant → span ignoré | U |
| TC-ADM-29 | GET /admin/api/devices/{uuid}/activity → 50 derniers spans | I |
| TC-ADM-30 | Trigger trim : > 200 events/device → purge automatique | I |
| TC-ADM-31 | Liste devices : colonne "dernière action" correctement renseignée | I |
| TC-ADM-13 | GET /admin/audit → liste des actions | I |
| TC-ADM-14 | GET /admin/api/campaigns/{id}/stats → fragment HTML valide | U |
| TC-ADM-15 | Toute action POST sans session → redirect login | U |

---

## 6. NFR — Checklist avant mise en production

```
□ Toutes les routes /admin/* requièrent @require_admin
□ Chaque POST/PUT/DELETE appelle audit_log() dans la même transaction DB
□ Upload binaire : extension whitelist + taille max 100 Mo
□ Pas d'interpolation de variable dans les requêtes SQL (psycopg2 paramétré)
□ Cookie session : HttpOnly, SameSite=Lax, pas Secure en dev (Secure en prod)
□ Timeout session 1h + refresh automatique si activité
□ ADMIN_SESSION_SECRET ≥ 32 bytes aléatoires (générer avec os.urandom(32).hex())
□ ADMIN_OIDC_CLIENT_SECRET dans les secrets (pas dans le code)
□ Tests TC-ADM-01 à TC-ADM-15 tous verts avant déploiement
□ Client Keycloak admin-dm-ui configuré : confidential, redirect_uri /admin/callback
□ Groupe admin-dm créé dans Keycloak et au moins un utilisateur assigné
```

---

## 7. Structure de fichiers finale

```
device-management/
  app/
    admin/
      __init__.py
      auth.py                 ← OIDC + session
      router.py               ← toutes les routes /admin/*
      helpers.py              ← audit_log(), db helpers admin
      templates/
        base.html
        dashboard.html
        devices.html
        device_detail.html
        cohorts.html
        cohort_edit.html
        feature_flags.html
        flag_detail.html
        artifacts.html
        campaigns.html
        campaign_new.html
        campaign_detail.html
        audit_log.html
  db/
    migrations/
      003_admin_audit.sql
  tests/
    test_admin_ui.py
  requirements.txt            ← ajouter: jinja2, python-multipart, aiofiles
```

---

## 7. Contraintes d'architecture

Ces contraintes sont **non-négociables** et doivent être vérifiées à chaque étape de l'implémentation.

### 7.1 Sécurité par défaut

| Contrainte | Mise en œuvre |
|---|---|
| Authentification OIDC/OAuth2 | Keycloak Authorization Code (client confidentiel `admin-dm-ui`), `@require_admin` sur **toutes** les routes sauf `/admin/login` et `/admin/callback` |
| Autorisation | Vérification de l'appartenance au groupe `admin-dm` à chaque requête (pas seulement à la connexion) |
| Validation des entrées | **Pydantic** pour tous les corps de requête POST/PUT ; rejet avec HTTP 422 si invalide |
| Protection CSRF | Token CSRF dans formulaire + cookie `SameSite=Strict` ; vérifié avant toute action d'écriture |
| Protection XSS | Auto-échappement Jinja2 activé (`autoescape=True`) — ne jamais utiliser `| safe` sauf sur du HTML généré en interne |
| Injection SQL | Uniquement des requêtes paramétrées psycopg2 — jamais de f-string dans les SQL |
| Upload fichiers | Whitelist d'extensions `.oxt/.xpi/.crx`, taille max 100 Mo, validation magic bytes, stockage hors webroot |
| Headers sécurité | `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Strict-Transport-Security` |
| Cookie session | `HttpOnly=True`, `Secure=True`, `SameSite=Strict`, TTL 1h, signé HMAC-SHA256 |

### 7.2 Architecture server-driven (pas de SPA)

- **Pas de framework JS front-end** (pas de React, Vue, Angular, Svelte).
- Toute la logique de rendu est côté serveur : FastAPI rend du HTML via Jinja2.
- HTMX est le seul outil JavaScript pour les interactions dynamiques (polling, partial updates, formulaires asynchrones).
- Les fragments HTML retournés par les routes HTMX (`hx-get`, `hx-post`) sont des **partials Jinja2**, pas du JSON.
- Chart.js est autorisé uniquement pour les graphiques (chargé depuis DSFR static ou CDN avec SRI hash).

### 7.3 Dépendances minimales

N'ajouter une dépendance que si elle remplace un bloc de >50 lignes de code maison difficile à maintenir.

**Autorisées :**
```
jinja2>=3.1.4
python-multipart>=0.0.9    # upload fichiers
aiofiles>=23.0.0           # lecture fichiers async
```

**Interdites :** SQLAlchemy ORM, Alembic, Celery, Redis, toute bibliothèque de session côté serveur (Flask-Session, etc.), Bootstrap, Tailwind, Alpine.js.

### 7.4 Modularité du backend Python

```
app/
  admin/
    router.py      ← routes uniquement, délègue à services
    auth.py        ← OIDC session, @require_admin, CSRF
    services/
      devices.py   ← requêtes SQL devices + télémétrie
      campaigns.py ← requêtes SQL campagnes
      flags.py     ← requêtes SQL feature flags
      cohorts.py   ← requêtes SQL cohortes
      artifacts.py ← upload + validation binaires
      audit.py     ← insert audit_log
    schemas.py     ← modèles Pydantic pour tous les inputs
    templates/     ← Jinja2 (voir §Stack technique)
    static/        ← DSFR + dm-admin.css
```

Règle : `router.py` ne contient **aucune** requête SQL directe.

### 7.5 Extensibilité API-first

Toutes les actions de l'UI doivent correspondre à une route JSON sous `/admin/api/` en plus de la route HTML :

| Route HTML | Route API JSON | Usage |
|---|---|---|
| `POST /admin/campaigns/{id}/activate` | `POST /admin/api/campaigns/{id}/activate` | Header `Accept: application/json` → `{"status": "active"}` |
| `POST /admin/campaigns/{id}/rollback` | `POST /admin/api/campaigns/{id}/rollback` | Automatisation CI/CD |
| `POST /admin/flags/{id}/overrides` | `POST /admin/api/flags/{id}/overrides` | Scripting |

Implémentation : dans chaque route, détecter `request.headers.get("Accept") == "application/json"` et retourner `JSONResponse` au lieu du template.

### 7.6 Traçabilité et non-répudiation

- **Toute** action d'écriture (POST/PUT/DELETE) doit appeler `audit_log()` **dans la même transaction** psycopg2 que la modification.
- Le `diff` enregistré doit contenir : `{"before": {...}, "after": {...}}` avec les valeurs **avant** et **après**.
- Les entrées d'audit ne sont **jamais supprimées** (pas de `DELETE` sur `admin_audit_log`).
- Les actions de lecture (GET) ne sont pas auditées sauf export CSV.

### 7.7 Failure modes à éviter

Ces erreurs d'architecture doivent être détectées et corrigées avant tout merge.

| # | Failure mode | Règle |
|---|---|---|
| 1 | Sur-ingénierie | Pas de microservices, event bus, message brokers. Rester **monolithique modulaire**. |
| 2 | Frontend complexe | Pas de React, Vue, Angular, build Node.js. SSR + HTMX uniquement. |
| 3 | Mélange des couches | Un module ne mélange jamais logique métier + SQL + HTTP + templates. Séparation stricte `endpoint → service → repository`. |
| 4 | Entrées non validées | Aucune donnée externe sans passage par un modèle Pydantic. |
| 5 | SQL dans les endpoints | Les endpoints n'accèdent jamais directement à la DB. Chemin obligatoire : `endpoint → service → repository`. |
| 6 | Gestion d'erreurs insuffisante | Toute exception est interceptée, journalisée côté serveur, retournée sous forme générique. Pas de stacktrace exposé au client. |
| 7 | Dépendances inutiles | N'ajouter une lib que si >50 lignes de code maison difficiles à maintenir. Vérifier d'abord stdlib Python + FastAPI + libs déjà présentes. |
| 8 | Secrets dans le code | Zéro hardcoded secret. Chargement via `os.getenv()` / Pydantic `BaseSettings`. `.env` non versionné. |
| 9 | Couplage métier ↔ UI | Les services dans `services/` doivent pouvoir être appelés par l'API, un CLI ou un agent — sans dépendance sur Jinja2 ou HTMX. |
| 10 | Code difficile à tester | Dépendances injectées (connexion DB en paramètre), services isolables, logique métier testable sans HTTP. |

**Règle de décision** : si plusieurs solutions sont possibles, choisir celle qui maximise — dans cet ordre — **sécurité**, **lisibilité**, **maintenabilité**, **auditabilité** plutôt que la sophistication technique.

> Pour chaque choix technique non trivial, ajouter un court commentaire dans le code expliquant ses avantages en matière de sécurité, de maintenabilité et de simplicité opérationnelle.

---

### 7.8 Exigences de sécurité de l'architecture

Le système doit être conçu **secure-by-design**. Le système doit passer une revue de sécurité de style OWASP. Toute décision de sécurité doit être documentée explicitement.

#### Authentification
- OAuth2 / OIDC avec JWT, intégration Keycloak obligatoire.
- Sessions à durée limitée (TTL 1h), invalidables, cookie `HttpOnly + Secure + SameSite=Strict`.

#### Autorisation
- Modèle **RBAC** — groupe `admin-dm` requis.
- Chaque endpoint vérifie les permissions explicitement. Refus par défaut (`deny by default`).

#### Validation des entrées
- Pydantic + typage Python strict pour toutes les entrées externes.
- Corps JSON limité à 1 Mo ; uploads limités à 100 Mo avec vérification extension + magic bytes.

#### Protection Web (OWASP Top 10)
- **CSRF** : token dans formulaire + vérification sur tous les POST/PUT/DELETE.
- **XSS** : `autoescape=True` Jinja2, jamais `| safe` sur données utilisateur.
- **Injection SQL** : exclusivement des requêtes paramétrées psycopg2.
- **Rate limiting** : 20 req/s par IP sur les routes d'auth (`/admin/login`, `/admin/callback`).

#### Secrets
- Jamais dans le code source ni dans les fichiers versionnés. `.env` dans `.gitignore`.
- Chargement via variables d'environnement ou gestionnaire de secrets.

#### Logging et audit
- Logs structurés (JSON) pour : authentification, accès ressources sensibles, erreurs critiques, modifications.
- Les logs ne contiennent jamais : mot de passe, token, donnée personnelle non anonymisée.
- Audit trail infalsifiable dans `admin_audit_log` (INSERT only, pas de UPDATE/DELETE).

#### Gestion des erreurs
- Pas de stacktrace côté client. Message générique + `request_id` pour corrélation.
- Détails loggés côté serveur avec niveau `ERROR`.

#### Dépendances
- Compatibles avec `pip-audit` / `safety` / scanners CVE. Zéro CVE critique accepté.

#### Protection des données
- HTTPS obligatoire en production (HSTS activé).
- Utilisateur PostgreSQL dédié `dm_admin` avec permissions minimales (SELECT/INSERT/UPDATE sur tables admin uniquement — pas de SUPERUSER ni CREATEDB).

#### Headers de sécurité HTTP (middleware FastAPI)
```python
@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self';"
    )
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response
```

---

### 7.9 Checklist avant PR

```
[ ] Aucun secret dans le code (SESSION_SECRET, CLIENT_SECRET via env uniquement)
[ ] .env absent du repo (.gitignore vérifié)
[ ] Toutes les routes admin ont @require_admin
[ ] Vérification CSRF sur tous les POST/PUT/DELETE
[ ] Pydantic schemas définis dans schemas.py pour chaque input
[ ] Aucune requête SQL avec f-string ou concaténation de chaîne
[ ] Autoescape Jinja2 activé, aucun | safe sur données utilisateur
[ ] Upload : extension + magic bytes + taille vérifiés
[ ] Audit log appelé dans la même transaction que chaque modification
[ ] Headers HTTP sécurité définis dans le middleware FastAPI
[ ] Stacktrace jamais exposé au client (messages génériques + request_id)
[ ] Logs structurés JSON, sans secret ni donnée sensible
[ ] Utilisateur DB dm_admin avec privilèges minimaux (pas de SUPERUSER)
[ ] Rate limiting actif sur /admin/login et /admin/callback
[ ] Architecture endpoint → service → repository respectée partout
[ ] Logique métier (services/) testable sans HTTP ni Jinja2
[ ] Tests TC-ADM-01 à TC-ADM-31 tous verts
[ ] NFR DSFR checklist (§6) validée
[ ] Aucune dépendance non autorisée (§7.3)
[ ] pip-audit / safety exécuté sans CVE critique
[ ] Chaque choix technique non trivial est commenté dans le code
```

---

## 8. Enregistrement du router dans `main.py`

```python
# À ajouter dans app/main.py après la création de `app`
from fastapi.templating import Jinja2Templates
from .admin.router import router as admin_router

app.include_router(admin_router, prefix="/admin")
templates = Jinja2Templates(directory="app/admin/templates")
```

---

## 9. Ordre d'implémentation recommandé

1. `003_admin_audit.sql` + migration (inclut `admin_audit_log` + `device_telemetry_events`)
2. `app/admin/auth.py` — session + OIDC flow
3. `_extract_and_store_telemetry_spans()` dans `main.py` — parsing OTLP + INSERT
4. `app/admin/router.py` — squelette avec `@require_admin` sur toutes les routes
5. `base.html` + `dashboard.html` (métriques statiques d'abord)
6. `campaigns.html` + `campaign_detail.html` + actions activate/pause/rollback
7. `feature_flags.html` + `flag_detail.html` + overrides
8. `cohorts.html` + création + estimation device count
9. `artifacts.html` + upload
10. `devices.html` — liste avec recherche par propriétaire, badge santé, colonne dernière action
11. `device_detail.html` — 5 onglets : Infos/Campagnes/Flags/Historique/Activité récente
12. `audit_log.html` + export CSV
13. HTMX polling sur dashboard + campaign_detail
14. `tests/test_admin_ui.py` — TC-ADM-01 à TC-ADM-31
15. Commit + validation NFR checklist

---

## 10. Références

- Mode opératoire : `docs/mode-operatoire-campagnes.md`
- Protocole plugin ↔ DM : `docs/plugin-dm-protocol-update-features.md`
- Schéma DB campagnes : `db/migrations/002_campaigns.sql`
- Keycloak client exemple : `keycloak/keycloak-bootstrap-client-example.json`
- Auth existante DM : `app/settings.py` (`auth_jwks_url`, `auth_audience`)
