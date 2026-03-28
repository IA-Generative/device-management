# Prompt — Catalogue de Plugins : Bouquet de services & Cycle de vie

> Version : 1.0 — 2026-03-28
> Perimetre : device-management (admin UI + API)
> Stack : FastAPI + Jinja2 + HTMX + DSFR
> Prerequis : admin UI existante, deploy wizard, base campaigns/cohorts/artifacts

---

## Vision

Transformer le Device Management d'un outil de deploiement unitaire en une **plateforme de gestion de catalogue de plugins** avec :

1. **Catalogue** — registre central des plugins disponibles, organise en bouquets de services
2. **Cycle de vie** — creation, distribution initiale, mises a jour progressives, deprecation, suppression
3. **Communication** — campagnes d'information, sondages express, notifications push aux utilisateurs
4. **Self-service** — portail utilisateur pour decouvrir, installer et gerer ses plugins

---

## 1. Catalogue de plugins

### Concept

Un **plugin** est une entite persistante dans le catalogue (ex: "Assistant Mirai LibreOffice"). Il a un nom, une description, une icone, une categorie, et un historique de **versions** (les artifacts existants). Un **bouquet** est un regroupement logique de plugins proposes ensemble (ex: "Pack Productivite" = Mirai LO + Mirai TB + Extension Chrome).

### Modele de donnees

```sql
-- Migration 005_plugin_catalog.sql

-- Plugins (entites du catalogue)
CREATE TABLE IF NOT EXISTS plugins (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(100) UNIQUE NOT NULL,          -- ex: "mirai-libreoffice"
    name VARCHAR(200) NOT NULL,                 -- ex: "Assistant Mirai LibreOffice"
    description TEXT,
    icon_url TEXT,                               -- URL ou path vers l'icone
    device_type VARCHAR(50) NOT NULL,           -- libreoffice, firefox, chrome, edge, matisse
    category VARCHAR(100) DEFAULT 'productivity', -- productivity, security, communication, tools
    intent TEXT,                                  -- intention / proposition de valeur (1-2 phrases)
    key_features JSONB DEFAULT '[]'::jsonb,     -- liste des fonctionnalites cles ["Redaction IA", "Mode hors-ligne", ...]
    changelog TEXT,                              -- historique global du plugin (markdown, toutes versions)
    homepage_url TEXT,                           -- lien doc/landing page
    support_email TEXT,
    publisher VARCHAR(200) DEFAULT 'DNUM',      -- editeur/equipe responsable
    visibility VARCHAR(20) DEFAULT 'public'     -- public, internal, hidden
        CHECK (visibility IN ('public', 'internal', 'hidden')),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'deprecated', 'removed')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Versions (lien plugin <-> artifact, enrichi avec notes de version)
CREATE TABLE IF NOT EXISTS plugin_versions (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    artifact_id INT REFERENCES artifacts(id),   -- NULL si version externe (lien direct)
    version VARCHAR(50) NOT NULL,
    release_notes TEXT,                         -- changelog en markdown
    download_url TEXT,                          -- lien direct si pas d'artifact interne
    min_host_version VARCHAR(50),              -- version minimale de l'hote (LO, TB, etc.)
    max_host_version VARCHAR(50),
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'deprecated', 'yanked')),
    distribution_mode VARCHAR(20) DEFAULT 'managed'
        CHECK (distribution_mode IN ('managed', 'download_link', 'store', 'manual')),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (plugin_id, version)
);

-- Bouquets (regroupements de plugins)
CREATE TABLE IF NOT EXISTS bundles (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,                -- ex: "Pack Productivite"
    description TEXT,
    icon_url TEXT,
    visibility VARCHAR(20) DEFAULT 'public'
        CHECK (visibility IN ('public', 'internal', 'hidden')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Composition des bouquets
CREATE TABLE IF NOT EXISTS bundle_plugins (
    bundle_id INT NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    is_required BOOLEAN DEFAULT true,          -- obligatoire ou optionnel dans le bouquet
    display_order INT DEFAULT 0,
    PRIMARY KEY (bundle_id, plugin_id)
);

-- Installations connues (tracking cote serveur)
CREATE TABLE IF NOT EXISTS plugin_installations (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id),
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    installed_version VARCHAR(50),
    installed_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active', 'inactive', 'uninstalled')),
    UNIQUE (plugin_id, client_uuid)
);

CREATE INDEX IF NOT EXISTS idx_plugin_installations_plugin ON plugin_installations(plugin_id);
CREATE INDEX IF NOT EXISTS idx_plugin_installations_client ON plugin_installations(client_uuid);
```

### Pages admin

| Route | Page | Description |
|-------|------|-------------|
| `GET /admin/catalog` | Liste du catalogue | Grille de cartes plugins avec filtres (categorie, statut, device_type) |
| `GET /admin/catalog/new` | Creer un plugin | Formulaire : nom, slug, description, intent, key_features, device_type, categorie, icone |
| `GET /admin/catalog/{slug}` | Fiche plugin | Detail avec onglets : Info, Versions, Installations, Statistiques |
| `POST /admin/catalog/{slug}/versions` | Publier une version | Upload artifact + notes de version + mode de distribution |
| `GET /admin/catalog/{slug}/versions/{version}` | Detail version | Notes, metriques d'adoption, lien deploiement 1-2-3 |
| `GET /admin/bundles` | Liste des bouquets | Cartes bouquets avec composition |
| `GET /admin/bundles/new` | Creer un bouquet | Drag & drop des plugins dans le bouquet |
| `GET /admin/bundles/{slug}` | Detail bouquet | Composition, stats d'adoption |

### Interface — Fiche plugin (maquette)

```
┌──────────────────────────────────────────────────────────────────┐
│  Catalogue > Assistant Mirai LibreOffice                         │
│                                                                  │
│  ┌────────┐  Assistant Mirai LibreOffice          [Actif]       │
│  │ [icon] │  Assistant IA pour la redaction dans LibreOffice     │
│  │  .oxt  │  Editeur : DNUM — Categorie : Productivite         │
│  └────────┘  docs | support                                     │
│                                                                  │
│  Intention : Augmenter la productivite des agents en integrant   │
│  l'IA generative directement dans leur outil de redaction        │
│  quotidien, sans changer leurs habitudes de travail.             │
│                                                                  │
│  Fonctionnalites cles :                                          │
│  [Redaction IA] [Reformulation] [Resume] [Traduction]           │
│  [Mode hors-ligne] [Correction orthographique]                   │
│                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │  1 245   │ │  v2.1.0  │ │  89.3%   │ │   12     │           │
│  │installs  │ │derniere  │ │adoption  │ │en retard │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│                                                                  │
│  [Versions]  [Changelog]  [Installations]  [Statistiques]        │
│  ─────────────────────────────────────────                       │
│                                                                  │
│  Versions                          [ + Publier une version ]     │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ v2.1.0  published  12/03  1 102 installs  [Deployer]     │  │
│  │ v2.0.3  published  28/02    987 installs  [Deprecier]    │  │
│  │ v2.0.2  deprecated 15/02    143 installs                  │  │
│  │ v1.9.0  yanked     01/01      0 installs                  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Derniere version : v2.1.0                                       │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ Notes de version :                                        │  │
│  │ - Correction du freeze au demarrage (telemetrie)          │  │
│  │ - Nouveau mode hors-ligne                                 │  │
│  │ - Amelioration des performances IA                        │  │
│  │                                                           │  │
│  │ Distribution : Geree (deploiement progressif)             │  │
│  │ Hote requis : LibreOffice >= 7.4                          │  │
│  │ SHA256 : 8e032cea...b720b62                               │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ─── Onglet [Changelog] ────────────────────────────────────    │
│                                                                  │
│  ## v2.1.0 — 12 mars 2026                                       │
│  - Correction du freeze au demarrage (telemetrie)                │
│  - Nouveau mode hors-ligne                                       │
│  - Amelioration des performances IA                              │
│                                                                  │
│  ## v2.0.3 — 28 fevrier 2026                                    │
│  - Correctif de securite (CVE-2026-XXXX)                         │
│  - Stabilite amelioree sur LibreOffice 24.8                      │
��                                                                  │
│  ## v2.0.0 — 15 janvier 2026                                    │
│  - Refonte de l'interface du panneau lateral                     │
│  - Support multi-modeles (GPT-OSS, DeepSeek)                    │
│  - Integration telemetrie OpenTelemetry                          │
│                                                                  │
│  [ Voir tout le changelog ]  [ Editer ]                          │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Cycle de vie des versions

### Etats d'une version

```
draft ──> published ──> deprecated ──> yanked
                  │                      ^
                  └──────────────────────┘
                     (retrait urgent)
```

| Etat | Signification | Effet sur les clients |
|------|--------------|----------------------|
| `draft` | En preparation, pas encore visible | Aucun — n'apparait pas dans le catalogue |
| `published` | Version active, deployable | Proposee en mise a jour / telechargement |
| `deprecated` | Remplacee par une version plus recente | Les clients existants ne sont pas forces de migrer, mais les nouveaux recoivent la derniere |
| `yanked` | Retiree (bug critique, securite) | Forcee en rollback si un artifact de remplacement est defini |

### Modes de distribution

| Mode | Description | Cas d'usage |
|------|-------------|-------------|
| `managed` | Deploiement via Device Management (1-2-3) | Plugins internes, mises a jour controlees |
| `download_link` | Lien de telechargement direct | Distribution initiale, plugins externes |
| `store` | Via le store officiel (Chrome Web Store, addons.mozilla.org) | Extensions navigateur publiques |
| `manual` | Installation manuelle (documentation) | Cas speciaux, plugins legacy |

### Actions disponibles par version

```
┌─────────────────────────────────────────────────────────────────────┐
│  Version v2.1.0 [published]                                        │
│                                                                     │
│  [ Deployer (1-2-3) ]  — Lance le wizard de deploiement pre-rempli │
│  [ Deprecier ]         — Marque comme obsolete                      │
│  [ Retirer (yank) ]    — Retrait d'urgence + rollback optionnel    │
│  [ Editer les notes ]  — Modifier le changelog                     │
│                                                                     │
│  Version v2.0.3 [deprecated]                                       │
│  [ Re-publier ]        — Remettre en version active                │
│  [ Retirer (yank) ]    — Retrait definitif                         │
└─────────────────────────────────────────────────────────────────────┘
```

### Integration avec le deploy wizard

Le bouton **"Deployer (1-2-3)"** sur une version pre-remplit le wizard :
- Device type → depuis le plugin
- Artifact → depuis la version
- Version → pre-remplie
- Nom de campagne → "MaJ {plugin.name} {version}"

Route : `GET /admin/deploy?plugin_id={id}&version_id={vid}`

---

## 3. Portail de distribution initiale

### Concept

Pour la **premiere installation** d'un plugin, le deploy wizard n'est pas adapte (le client n'est pas encore enrole). Il faut un portail public (ou semi-public) qui propose :

- La fiche du plugin avec description et captures d'ecran
- Un bouton de telechargement direct (lien vers l'artifact ou le store)
- Les instructions d'installation selon la plateforme
- Le lien d'enrolement Device Management pour les mises a jour futures

### Page publique (hors admin)

```
GET /catalog                        → Liste publique des plugins
GET /catalog/{slug}                 → Fiche publique du plugin
GET /catalog/{slug}/download        → Telechargement de la derniere version published
GET /catalog/bundles/{slug}         → Fiche publique du bouquet
```

### Interface — Page publique plugin

```
┌──────────────────────────────────────────────────────────────────┐
│  ┌────────┐  Assistant Mirai LibreOffice              v2.1.0    │
│  │ [icon] │                                                      │
│  └────────┘  Augmenter la productivite des agents en integrant  │
│              l'IA generative directement dans leur outil de      │
│              redaction quotidien.                                 │
│                                                                  │
│  [Redaction IA] [Reformulation] [Resume] [Traduction]           │
│  [Mode hors-ligne] [Correction orthographique]                   │
│                                                                  │
│  [ Telecharger (.oxt) ]    [ Instructions d'installation ]      │
│                                                                  │
│  Compatibilite : LibreOffice 7.4+                               │
│  Taille : 2.3 Mo                                                │
│  Derniere mise a jour : 12 mars 2026                            │
│                                                                  │
│  ───────────────────────────────────────────────                 │
│  Nouveautes v2.1.0 :                                            │
│  - Correction du freeze au demarrage                             │
│  - Nouveau mode hors-ligne                                       │
│  - Amelioration des performances IA                              │
│                                                                  │
│  ───────────────────────────────────────────────                 │
│  Mises a jour automatiques :                                     │
│  Ce plugin se met a jour automatiquement via Device Management.  │
│  Lors du premier lancement, connectez-vous avec votre compte     │
│  pour activer les mises a jour.                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. Campagnes de communication

### Concept

Au-dela du deploiement technique, les administrateurs doivent pouvoir communiquer avec les utilisateurs de plugins : annonces, alertes de securite, sondages de satisfaction. Ces messages sont affiches dans le plugin via l'endpoint de configuration.

### Modele de donnees

```sql
-- Migration 006_communications.sql

-- Types de communication
-- 'announcement' : message informatif (ex: nouvelle version disponible)
-- 'alert'        : alerte importante (ex: vulnerabilite, maintenance)
-- 'survey'       : sondage express (1 question, choix multiples)
-- 'changelog'    : notes de version (affichees apres mise a jour)

CREATE TABLE IF NOT EXISTS communications (
    id SERIAL PRIMARY KEY,
    type VARCHAR(20) NOT NULL
        CHECK (type IN ('announcement', 'alert', 'survey', 'changelog')),
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,                         -- contenu markdown
    priority VARCHAR(10) DEFAULT 'normal'
        CHECK (priority IN ('low', 'normal', 'high', 'critical')),

    -- Ciblage
    target_plugin_id INT REFERENCES plugins(id),  -- NULL = tous les plugins
    target_cohort_id INT REFERENCES cohorts(id),  -- NULL = tous les utilisateurs
    target_bundle_id INT REFERENCES bundles(id),  -- NULL = pas de ciblage bouquet
    min_plugin_version VARCHAR(50),               -- n'afficher qu'a partir de cette version
    max_plugin_version VARCHAR(50),               -- n'afficher que jusqu'a cette version

    -- Planification
    starts_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,                       -- NULL = pas d'expiration

    -- Sondage (si type = 'survey')
    survey_question TEXT,
    survey_choices JSONB,                         -- ["Oui", "Non", "Sans avis"]
    survey_allow_multiple BOOLEAN DEFAULT false,

    -- Etat
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'paused', 'completed', 'expired')),
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Reponses aux sondages
CREATE TABLE IF NOT EXISTS survey_responses (
    id SERIAL PRIMARY KEY,
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    choices JSONB NOT NULL,                      -- indices des choix selectionnes
    comment TEXT,                                -- commentaire libre optionnel
    responded_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (communication_id, client_uuid)
);

-- Acquittement des messages (pour ne pas re-afficher)
CREATE TABLE IF NOT EXISTS communication_acks (
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    acked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (communication_id, client_uuid)
);

CREATE INDEX IF NOT EXISTS idx_communications_status ON communications(status, starts_at);
CREATE INDEX IF NOT EXISTS idx_communications_plugin ON communications(target_plugin_id);
CREATE INDEX IF NOT EXISTS idx_survey_responses_comm ON survey_responses(communication_id);
```

### Pages admin

| Route | Page | Description |
|-------|------|-------------|
| `GET /admin/communications` | Liste | Toutes les communications avec filtres (type, status, plugin) |
| `GET /admin/communications/new` | Creer | Formulaire assiste selon le type |
| `GET /admin/communications/{id}` | Detail | Preview + statistiques (vus, acquittes, reponses sondage) |
| `GET /admin/communications/{id}/results` | Resultats sondage | Graphique des reponses + commentaires |

### Interface — Creation d'une communication

```
┌──────────────────────────────────────────────────────────────────┐
│  Nouvelle communication                                          │
│                                                                  │
│  Type :                                                          │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐   │
│  │ Annonce    │ │ Alerte     │ │ Sondage    │ │ Changelog  │   │
│  │  [ ● ]    │ │  [   ]     │ │  [   ]     │ │  [   ]     │   │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘   │
│                                                                  │
│  Titre : [ Nouvelle version 2.1 disponible          ]           │
│                                                                  │
│  Message (markdown) :                                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ La version 2.1 de l'assistant Mirai est disponible.      │  │
│  │                                                           │  │
│  │ **Nouveautes :**                                          │  │
│  │ - Mode hors-ligne                                         │  │
│  │ - Performances ameliorees                                 │  │
│  │                                                           │  │
│  │ La mise a jour est automatique.                           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Priorite : (●) Normale  ( ) Haute  ( ) Critique               │
│                                                                  │
│  Ciblage :                                                       │
│  Plugin : [ Assistant Mirai LibreOffice  ▼ ]                    │
│  Groupe : [ Tous les utilisateurs        ▼ ]                    │
│                                                                  │
│  Planification :                                                 │
│  Debut : [ 2026-03-28 10:00 ]                                   │
│  Fin :   [ 2026-04-15 23:59 ] (optionnel)                      │
│                                                                  │
│              [ Previsualiser ]  [ Publier ]                      │
└──────────────────────────────────────────────────────────────────┘
```

### Interface — Sondage express

```
┌──────────────────────────────────────────────────────────────────┐
│  Sondage express                                                 │
│                                                                  │
│  Question :                                                      │
│  [ Etes-vous satisfait de la nouvelle interface ?     ]          │
│                                                                  │
│  Choix (un par ligne) :                                          │
│  ┌─────────────────────────────────┐                             │
│  │ Tres satisfait                  │                             │
│  │ Satisfait                       │                             │
│  │ Neutre                          │                             │
│  │ Insatisfait                     │                             │
│  │ Tres insatisfait                │                             │
│  └─────────────────────────────────┘                             │
│                                                                  │
│  [ ] Autoriser la selection multiple                             │
│  [ ] Ajouter un champ commentaire libre                          │
│                                                                  │
│  Ciblage : Plugin [ Mirai LO ▼ ]  Version >= [ 2.1.0 ]         │
│                                                                  │
│              [ Previsualiser ]  [ Publier ]                      │
└──────────────────────────────────────────────────────────────────┘
```

### Interface — Resultats sondage

```
┌──────────────────────────────────────────────────────────────────┐
│  Sondage : Etes-vous satisfait de la nouvelle interface ?        │
│  Actif depuis 5 jours — 342 reponses / 1 200 cibles (28.5%)    │
│                                                                  │
│  ██████████████████████████████████░░░  Tres satisfait   38%    │
│  ████████████████████████░░░░░░░░░░░░  Satisfait         31%    │
│  █████████████░░░░░░░░░░░░░░░░░░░░░░  Neutre            18%    │
│  █████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  Insatisfait         9%    │
│  ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  Tres insatisfait    4%    │
│                                                                  │
│  Commentaires recents :                                          │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ "Le mode hors-ligne est super pratique" — alice@mi...     │  │
│  │ "J'aimerais un raccourci clavier" — bob@mini...           │  │
│  │ "Parfois lent avec les gros documents" — carol@mi...      │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  [ Exporter CSV ]  [ Clore le sondage ]                         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. Integration avec l'endpoint de configuration

### Enrichissement du JSON config

L'endpoint `GET /config/{device}/config.json` est deja le point de contact principal avec les plugins. On l'enrichit avec les communications actives :

```json
{
  "meta": { "schema_version": 3, "..." : "..." },
  "config": { "...": "..." },
  "update": { "...": "..." },
  "features": { "...": "..." },
  "communications": [
    {
      "id": 42,
      "type": "announcement",
      "title": "Nouvelle version 2.1 disponible",
      "body": "La mise a jour est automatique...",
      "priority": "normal",
      "starts_at": "2026-03-28T10:00:00Z",
      "expires_at": "2026-04-15T23:59:00Z"
    },
    {
      "id": 43,
      "type": "survey",
      "title": "Satisfaction",
      "survey_question": "Etes-vous satisfait ?",
      "survey_choices": ["Tres satisfait", "Satisfait", "Neutre", "Insatisfait"],
      "priority": "normal"
    }
  ]
}
```

### Nouveaux endpoints API

```
POST /communications/{id}/ack              → Acquitter (ne plus afficher)
POST /communications/{id}/survey/respond   → Repondre au sondage
GET  /catalog/api/plugins                  → Liste publique JSON des plugins
GET  /catalog/api/plugins/{slug}           → Detail plugin + derniere version
GET  /catalog/api/bundles                  → Liste publique des bouquets
```

---

## 6. Suppression d'un plugin

### Workflow de suppression

La suppression est un processus en plusieurs etapes pour eviter les pertes accidentelles :

```
active → deprecated (annonce de fin de vie, delai configurable)
       → removed (masque du catalogue, mises a jour stoppees)
       → [purge manuelle] (suppression des donnees apres retention)
```

### Etape 1 : Depreciation

- Le plugin est marque `deprecated` dans le catalogue
- Une communication automatique de type `alert` est creee :
  "Ce plugin sera retire le {date}. Veuillez {action alternative}."
- Les nouvelles installations sont bloquees
- Les installations existantes continuent de fonctionner

### Etape 2 : Retrait

- Le plugin passe en `removed`
- Le telechargement est desactive
- Les campagnes actives sont automatiquement stoppees
- Une communication finale est envoyee

### Etape 3 : Purge (optionnelle)

- Suppression des artifacts (S3/local)
- Suppression des metriques d'installation
- Conservation du journal d'audit

### Interface

```
┌──────────────────────────────────────────────────────────────────┐
│  Supprimer : Assistant Mirai LibreOffice                         │
│                                                                  │
│  ⚠ Cette action est irreversible.                               │
│                                                                  │
│  Etape 1 : Deprecier le plugin                                   │
│  [ ] Envoyer une communication de fin de vie                     │
│  Date de retrait prevue : [ 2026-04-30 ]                         │
│  Message personnalise :                                          │
│  [ Ce plugin est remplace par la version 3.0... ]                │
│                                                                  │
│  Etape 2 : Retrait definitif (apres la date)                     │
│  [ ] Stopper toutes les campagnes actives                        │
│  [ ] Supprimer les artifacts du stockage                         │
│                                                                  │
│             [ Annuler ]  [ Deprecier maintenant ]                │
└──────────────────────────────────────────────────────────────────┘
```

---

## 7. Resume des fichiers a creer/modifier

### Nouveaux fichiers

| Fichier | Description |
|---------|-------------|
| `db/migrations/005_plugin_catalog.sql` | Tables plugins, plugin_versions, bundles, bundle_plugins, plugin_installations |
| `db/migrations/006_communications.sql` | Tables communications, survey_responses, communication_acks |
| `app/admin/services/catalog.py` | CRUD plugins, versions, bouquets, installations |
| `app/admin/services/communications.py` | CRUD communications, sondages, acquittements |
| `app/admin/templates/catalog.html` | Liste du catalogue (grille de cartes) |
| `app/admin/templates/catalog_plugin.html` | Fiche plugin (onglets versions/installations/stats) |
| `app/admin/templates/catalog_plugin_new.html` | Formulaire creation plugin |
| `app/admin/templates/bundles.html` | Liste et detail des bouquets |
| `app/admin/templates/communications.html` | Liste des communications |
| `app/admin/templates/communication_new.html` | Formulaire creation (annonce/alerte/sondage/changelog) |
| `app/admin/templates/communication_detail.html` | Detail + resultats sondage |
| `app/catalog_router.py` | Routes publiques `/catalog/*` (hors admin) |
| `app/admin/templates/public_catalog.html` | Page publique catalogue |
| `app/admin/templates/public_plugin.html` | Fiche publique plugin + telechargement |

### Fichiers modifies

| Fichier | Modification |
|---------|-------------|
| `app/admin/router.py` | Ajouter routes `/admin/catalog/*`, `/admin/bundles/*`, `/admin/communications/*` |
| `app/admin/templates/base.html` | Ajouter "Catalogue" et "Communications" dans la nav |
| `app/main.py` | Enrichir endpoint config avec `communications[]`, ajouter routes publiques `/catalog/*` |
| `app/admin/templates/deploy_wizard.html` | Accepter `?plugin_id=&version_id=` pour pre-remplir |

### Ordre d'implementation recommande

1. **Phase 1 — Catalogue** : migration 005 + services/catalog.py + pages admin catalogue + fiche plugin
2. **Phase 2 — Cycle de vie** : gestion des versions (publish/deprecate/yank) + integration deploy wizard
3. **Phase 3 — Communications** : migration 006 + services/communications.py + pages admin + enrichissement config
4. **Phase 4 — Portail public** : routes `/catalog/*` + pages publiques + telechargement
5. **Phase 5 — Bouquets** : gestion des bundles + pages admin + portail public

---

## 8. Navigation mise a jour

```
Tableau de bord | Deploiement 1-2-3 | Catalogue | Communications | Appareils | Campagnes | ...
```

Le **Catalogue** devient le hub central. Le **Deploiement 1-2-3** est accessible depuis le catalogue (bouton "Deployer" sur chaque version) et reste aussi en acces direct dans la nav.
