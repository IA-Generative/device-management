# Prompt — Pages publiques du catalogue dans Device Management

> Version : 1.0 — 2026-03-29
> Perimetre : device-management (3 routes HTML publiques)
> Stack : FastAPI + Jinja2, CSS DSFR (Marianne), pas de build JS
> Prerequis : API catalogue fonctionnelle (`/catalog/api/plugins`, `/catalog/api/plugins/{slug}`)

---

## Contexte

Le Device Management dispose d'un catalogue admin (`/admin/catalog`) et d'une API JSON publique (`/catalog/api/plugins`). Il manque les **pages HTML publiques** permettant aux utilisateurs finaux de :

- Decouvrir les plugins disponibles
- Consulter la fiche d'un plugin (description, fonctionnalites, changelog)
- Telecharger la derniere version pour une premiere installation

Ces pages sont le point d'entree pour les utilisateurs **avant enrollment** — le deploy wizard ne les concerne pas encore.

### Donnees disponibles en DB

Toutes les donnees necessaires sont deja en base et accessibles via l'API JSON :

```
GET /catalog/api/plugins          → liste avec slug, name, intent, key_features, icon_url, latest_version, install_count, maturity
GET /catalog/api/plugins/{slug}   → detail complet + description, changelog_summary, homepage_url, doc_url, license, support_email
GET /catalog/api/plugins/{slug}/icon.{ext}  → binaire de l'icone (PNG, JPG, SVG)
```

Les artifacts (binaires) sont stockes dans `plugin_versions` lies a `artifacts` avec `s3_path` (chemin local ou S3).

---

## Routes a creer

| Route | Methode | Description |
|-------|---------|-------------|
| `GET /catalog` | HTML | Grille de plugins avec badges, filtres, stats |
| `GET /catalog/{slug}` | HTML | Fiche complete du plugin |
| `GET /catalog/{slug}/download` | Redirect/binaire | Telechargement de la derniere version published |

---

## 1. Page catalogue — `GET /catalog`

**Layout** : page autonome (pas dans l'admin), style DSFR, police Marianne, couleurs `#000091` / `#F5F5FE`.

```
┌──────────────────────────────────────────────────────────────────┐
│  [Logo MIrAI]   Catalogue de plugins              [Rechercher]  │
│                                                                  │
│  Filtres : [Tous] [Productivite] [Securite] [Communication]     │
│                                                                  │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │ [icon]          │  │ [icon]          │  │ [icon]          │ │
│  │ MIrAI LO        │  │ Matisse TB      │  │ Extension Chrome│ │
│  │ v0.2.1 · Stable │  │ v1.3.0 · Beta   │  │ v0.1.0 · Dev   │ │
│  │                  │  │                  │  │                  │ │
│  │ Assister les     │  │ Messagerie       │  │ Navigation       │ │
│  │ redacteurs...    │  │ securisee...     │  │ augmentee...     │ │
│  │                  │  │                  │  │                  │ │
│  │ [Writer] [Calc]  │  │ [Chiffrement]   │  │ [Recherche IA]  │ │
│  │                  │  │                  │  │                  │ │
│  │ 245 installs     │  │ 1 203 installs  │  │ 12 installs     │ │
│  │ [Voir la fiche]  │  │ [Voir la fiche] │  │ [Voir la fiche] │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘ │
│                                                                  │
│  ── Propulse par Device Management ──                           │
└──────────────────────────────────────────────────────────────────┘
```

**Specifications** :
- Grille responsive : 3 colonnes desktop, 2 tablette, 1 mobile
- Chaque carte : icone (via `/catalog/api/plugins/{slug}/icon.png`), nom, version, badge maturite (couleur selon dev/alpha/beta/pre-release/release), intent tronquee, tags key_features (3-5 max), nombre d'installs, lien vers la fiche
- Badges maturite : `dev` rouge, `alpha` orange, `beta` jaune, `pre-release` bleu, `release` vert
- Filtres par categorie (query param `?category=productivity`)
- Seuls les plugins `status='active'` et `visibility IN ('public','internal')` sont affiches
- Pas de JS obligatoire, les filtres sont des liens classiques

**Implementation** :
- Template Jinja2 : `app/catalog/templates/catalog_index.html`
- Route dans `app/main.py` (pas dans l'admin router) : `@app.get("/catalog")`
- La route fait la query DB directement (comme `api_public_plugins` mais retourne du HTML)
- CSS inline ou fichier dedie `app/catalog/static/catalog.css` — pas de dependance a dm-admin.css

---

## 2. Fiche plugin — `GET /catalog/{slug}`

```
┌──────────────────────────────────────────────────────────────────┐
│  < Retour au catalogue                                          │
│                                                                  │
│  ┌────────┐  MIrAI — IA'ssistant LibreOffice          v0.2.1   │
│  │ [icon] │  par DTNUM · Licence MPL-2.0                        │
│  │  64x64 │  LibreOffice · Productivite · Stable                │
│  └────────┘                                                      │
│                                                                  │
│  Assister les redacteurs dans la production de documents         │
│  professionnels en integrant l'IA directement dans LibreOffice.  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  [ Telecharger v0.2.1 (.oxt) ]   [ Documentation ]      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  Fonctionnalites                                                 │
│  [Generer la suite] [Modifier la selection] [Ajuster longueur]  │
│  [Resumer] [Reformuler] [Transformer colonne] [Formules IA]    │
│  [Analyser plage] [Reference fonctions] [Deploiement auto]     │
│                                                                  │
│  ────────────────────────────────────────────                    │
│                                                                  │
│  Description                                                     │
│  Extension LibreOffice integrant un assistant IA dans Writer     │
│  et Calc. Generation, modification, resume, reformulation,      │
│  ajustement de longueur, formules IA et analyse de donnees.     │
│                                                                  │
│  ────────────────────────────────────────────                    │
│                                                                  │
│  Nouveautes v0.2.1                                               │
│  - Telemetrie : attribut plugin.action condense sur chaque trace│
│  - Telemetrie : attribut trigger.source identifie la source     │
│  - Telemetrie : ajout traces lifecycle                          │
│  - Suppression entree menu Generer la suite                     │
│                                                                  │
│  ────────────────────────────────────────────                    │
│                                                                  │
│  Mises a jour automatiques                                       │
│  Ce plugin se met a jour automatiquement via Device Management.  │
│  Lors du premier lancement, connectez-vous avec votre compte     │
│  pour activer les mises a jour.                                  │
│                                                                  │
│  ────────────────────────────────────────────                    │
│  Informations                                                    │
│  Editeur : DTNUM          Licence : MPL-2.0                     │
│  Support : fabrique-numerique@interieur.gouv.fr                  │
│  Source  : github.com/IA-Generative/...                          │
│  Installs : 0             Derniere MAJ : 29 mars 2026           │
└──────────────────────────────────────────────────────────────────┘
```

**Specifications** :
- Header avec icone, nom, version, editeur, licence, badges
- Boutons CTA : telechargement (lien vers `/catalog/{slug}/download`) + documentation (`doc_url`)
- Section fonctionnalites : tags `key_features` en pills colorees
- Section description : texte complet
- Section changelog : `release_notes` de la derniere version published
- Section "Mises a jour automatiques" : texte fixe expliquant l'enrollment
- Section informations : editeur, licence, support, source, installs, date
- L'extension du fichier est deduite du `device_type` : libreoffice→.oxt, firefox→.xpi, chrome→.crx

**Implementation** :
- Template Jinja2 : `app/catalog/templates/catalog_detail.html`
- Route dans `app/main.py` : `@app.get("/catalog/{slug}")` — attention a l'ordre des routes, cette route doit etre **apres** `/catalog/api/plugins` pour ne pas capturer les sous-chemins API
- Query DB : `SELECT * FROM plugins WHERE slug = %s AND status = 'active'` + version + installs

---

## 3. Telechargement — `GET /catalog/{slug}/download`

- Resout la derniere `plugin_version` en statut `published` pour le plugin
- Si `distribution_mode = 'managed'` et un `artifact_id` existe : sert le binaire depuis le stockage local (`s3_path`) avec `Content-Disposition: attachment; filename="{slug}-{version}.{ext}"`
- Si `distribution_mode = 'download_link'` : redirect 302 vers `download_url`
- Si `distribution_mode = 'store'` : redirect 302 vers `download_url` (store officiel)
- Si aucune version published : HTTP 404 avec message "Aucune version disponible"

**Implementation** :
- Route dans `app/main.py` : `@app.get("/catalog/{slug}/download")`
- Extensions par device_type : `{"libreoffice": "oxt", "firefox": "xpi", "chrome": "crx", "edge": "crx", "matisse": "xpi"}`

---

## Style CSS

Les pages publiques utilisent le DSFR (Systeme de Design de l'Etat) en version allegee :
- Police : `Marianne` (deja chargee dans le projet)
- Couleur primaire : `#000091`
- Fond clair : `#F5F5FE`
- Badges : fond colore + texte blanc, border-radius 4px
- Boutons : style DSFR (`.fr-btn`) ou equivalent simplifie
- Pas de Tailwind, pas de framework JS, pas de build step
- Responsive via CSS Grid + media queries

---

## Arborescence fichiers

```
app/
  catalog/
    __init__.py           (vide)
    templates/
      catalog_base.html   (layout commun : header, footer, CSS)
      catalog_index.html  (grille de plugins)
      catalog_detail.html (fiche plugin)
  main.py                 (ajout des 3 routes)
```

---

## Contraintes

- Les routes `/catalog/api/*` et `/catalog/icons/*` existent deja — ne pas les casser
- L'ordre des routes FastAPI compte : les routes les plus specifiques doivent etre declarees en premier
- Pas d'authentification sur ces pages (publiques)
- Les icones sont stockees en base (data URL base64 dans `plugins.icon_url`) et servies par `/catalog/api/plugins/{slug}/icon.{ext}`
