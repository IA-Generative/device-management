# ADR-0001 : Vue d'ensemble — fonctionnement, architecture, distribution de valeur et modèle de sécurité

**Date** : 2026-06-02
**Statut** : En vigueur
**Auteurs** : eric.tiquet + Claude Opus 4.8
**Portée** : ADR transverse — point d'entrée des décisions d'architecture. Les ADR détaillés
([adr-product-architecture](adr-product-architecture.md), [adr-dgx-deployment](adr-dgx-deployment.md))
approfondissent chaque décision.

---

## Contexte

### Le problème

Équiper les postes d'agents d'extensions bureautiques (un assistant IA dans LibreOffice,
Thunderbird, un navigateur) ne pose pas de difficulté à l'**installation** : un outil de gestion de
parc (WAPT, SCCM, Intune) sait poser et mettre à jour un paquet sur une flotte de machines. La
difficulté est **ailleurs et après** : configurer ces extensions par environnement, déployer leurs
versions progressivement, contrôler les accès, et leur ouvrir un accès **sécurisé** aux services
d'IA — le tout à un **rythme rapide, piloté par les retours des utilisateurs**, sur une flotte qu'on
ne réinstalle pas à chaque ajustement, sans exposer de secret sur les postes, et en gardant la
maîtrise complète de la politique de magasin.

### En deux mots

`device-management` (DM) est une plateforme de **gestion du cycle de vie de plugins bureautiques**
(LibreOffice, Thunderbird, Firefox, Chrome/Edge) qui répond à ce problème. Ce n'est **pas un app
store** et ce n'est **pas un outil de gestion de parc** : DM gère ce qui se passe *après*
l'installation — configuration, mises à jour progressives, télémétrie, accès authentifié aux API
externes, et distribution de binaires. Le « comment cela marche » est détaillé en §1 (fonctionnement
d'ensemble) et §3 (distribution de valeur vers le plugin).

Le système est déployé dans deux environnements aux contraintes opposées : un cluster managé
(Scaleway Kapsule) et un environnement on-premise sous fortes contraintes (DGX : proxy sortant, WAF,
pas de registry direct — détaillé dans [adr-dgx-deployment](adr-dgx-deployment.md)).

Cet ADR formalise quatre choses :
1. **Le fonctionnement d'ensemble** du système ;
2. **Les choix d'architecture** structurants et leurs justifications ;
3. **Le modèle de distribution de valeur** vers les plugins ;
4. **Le modèle de sécurité** de bout en bout.

---

## Positionnement : complémentarité avec la gestion de parc

DM se définit autant par ce qu'il **n'est pas** que par ce qu'il fait. **Ce n'est pas un outil de
télédistribution de parc** (WAPT, SCCM, Intune) et **ce n'est pas un magasin public** (Chrome Web
Store, AMO). Il est le **complément** du premier et l'**alternative souveraine** au second.

**Frontière avec l'outil de gestion de parc.** Les deux opèrent sur deux plans distincts et se
passent le relais à une frontière nette :

| Plan | Outil de gestion de parc (WAPT…) | Device Management |
|---|---|---|
| **Unité gérée** | Paquet binaire (`.oxt`/`.xpi`/`.crx`), une version | Configuration + fonctionnalité (feature flags, directive d'update *in-plugin*) |
| **Clé de ciblage** | Machine (inventaire de parc) | Identité IAM (groupe Keycloak, motif e-mail, cohorte, pourcentage) |
| **Déclencheur** | Déploiement initial + montées de version **majeures** | Tout le cycle de vie **post-installation** |
| **Cadence** | Cycle d'empaquetage (sysadmin) | Cycle produit (changement serveur, pris au prochain `GET /config`, sans réinstall) |
| **Retour terrain** | Statut d'installation | Télémétrie d'**usage fonctionnel** (adoption, erreurs, pertinence) |
| **Config & secrets** | Figés dans le paquet, sur le disque du poste | Servis dynamiquement ; secrets jamais sur le poste (relay A4), révocation atomique |

```
   ┌────────────────────────────────┐
   │      Gestionnaire de parc      │   ①  installe / met à jour le PAQUET
   │     (WAPT · SCCM · Intune)     │       (.oxt/.xpi/.crx) : déploiement
   └───────────────┬────────────────┘       initial + montées de version majeures
                   │ ①
                   ▼
   ┌────────────────────── Poste agent ───────────────────────┐
   │   ┌──────────────┐    héberge    ┌─────────────────────┐  │
   │   │ Hôte         │ ────────────▶ │ Extension (plug-in) │  │
   │   │ LibreOffice  │               │ « Assistant MirAI » │  │
   │   │ Thunderbird  │               │ s'exécute dans      │  │
   │   │ Navigateur   │               │ l'hôte              │  │
   │   └──────────────┘               └──────────┬──────────┘  │
   └─────────────────────────────────────────────┼────────────┘
                                                 │ ②  configuration, directive
                                                 │    d'update, feature flags
                                                 │    (par cohorte), télémétrie
                                                 ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Device Management (DM) — cycle de vie APRÈS installation │
   │  • configuration par environnement                       │
   │  • déploiement progressif (canari) + feature toggling    │
   │  • télémétrie d'usage   • relais sécurisé (médiation)     │
   └───────────────────────────┬──────────────────────────────┘
                               │ ③  médiation authentifiée
                               │    (secrets jamais sur le poste)
                               ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Services tiers / fournisseurs                           │
   │  Keycloak (SSO) · LLM (IA) · API métier · Stockage S3     │
   └──────────────────────────────────────────────────────────┘
```

> **①** Le gestionnaire de parc pose le binaire sur l'hôte (initial + montées majeures), *centré
> machine*. **②** L'extension dialogue avec DM pour tout le cycle de vie post-installation, sans
> réinstaller, *centré identité*. **③** DM médiatise l'accès aux services tiers (point de médiation
> unique, secrets côté serveur). **Frontière de responsabilité** : le parc s'arrête à l'installation
> du paquet, DM gouverne la suite, l'extension s'exécute dans son hôte et consomme.

**Conséquence d'architecture.** Ce positionnement justifie *a posteriori* plusieurs décisions
structurantes : la **config comme service** (A3) existe précisément parce que DM agit *après*
l'installation, sans ré-empaqueter ; le **ciblage par cohortes IAM** (A8) découle du choix de la clé
*identité* plutôt que *machine* ; la **télémétrie intégrée** (A10) est la boucle de retour fonctionnelle
absente des outils de parc ; le **relay authentifié** (A4) permet une config dynamique *et* sécurisée là
où un paquet figerait les secrets sur le disque. Le déploiement initial du paquet (et ses montées
majeures) **peut rester confié à l'outil de parc** : DM prend le relais sur la suite.

**Feature toggling par cohorte (test / kill switch).** Conséquence directe de la conjonction *granularité
fonctionnelle* (unité = la fonctionnalité) et *clé identité* (ciblage par cohorte) : les *feature flags*
(`feature_flags` + `feature_flag_overrides`, surcharge par cohorte avec `min_plugin_version`) sont
injectés dans le `config.json` à l'étape 10 du pipeline. On peut donc **activer une fonctionnalité pour une
cohorte pilote** (test progressif découplé de la version du binaire), ou la **désactiver instantanément**
(*kill switch*) en cas de régression — **sans réinstallation ni rollback de paquet**. C'est le pendant
*fonctionnel* du déploiement progressif *binaire* (A8) : le canari porte sur la fonctionnalité, pas
seulement sur la version, et se pilote sur la même clé de cohorte. Couplé à la télémétrie (A10), il ferme
la boucle « activer sur un échantillon → mesurer l'usage → étendre ou retirer ».

**Contrat et répartition des responsabilités (tiers).** Le couple **plugin + DM** forme un contrat
explicite face aux applications et fournisseurs tiers : le plugin **déclare** son besoin de config
(`dm-config.json`) et **consomme** via le relay ; DM est le **point de médiation unique** (credentials,
quotas, autorisations, audit, masquage) ; le fournisseur tiers **expose** sa capacité sans connaître les
postes ni détenir d'identité côté agent. Frontière de responsabilité = la couche relay. Les
**politiques d'autorisation et d'exception** (éligibilité des postes au déploiement, dérogations) restent
**à la main du gestionnaire de parc**, en amont de DM, qui opère dans ce cadre.

**Souveraineté.** L'organisation conserve **100 %** de la politique de son magasin (catalogue, maturité,
modes d'accès, rythme/cible de déploiement, retrait), sans dépendance à un store externe ni export de
l'inventaire d'usage hors du périmètre maîtrisé.

**Objectif directeur — boucle de feedback courte.** L'ensemble de ces choix converge vers une finalité
unique : **soutenir un rythme de développement/déploiement très rapide pour prendre en compte le feedback
utilisateur au plus tôt**. Le système instrumente les deux moitiés de la boucle. Côté *capter* : feedback
**implicite** via la télémétrie d'usage (A10, `device_telemetry_events`) et feedback **explicite** via les
sondages/communications (`communications`, `survey_responses`). Côté *agir* : config dynamique (A3),
*feature toggling* par cohorte et déploiement progressif (A8) — tous actionnables **côté serveur, sans
ré-empaquetage ni réinstallation**. La conséquence est un cycle **mesurer → décider → déployer** dont la
latence n'est plus celle de la chaîne d'empaquetage mais celle d'un changement de configuration, validé
d'abord sur une cohorte pilote puis étendu. C'est la justification produit transverse des décisions A3,
A8 et A10.

---

## 1. Fonctionnement d'ensemble

### 1.1 Un monolithe FastAPI multi-modal

DM est **une seule base de code Python (FastAPI)**, exécutée selon 4 modes via `DM_RUNTIME_MODE` :

| Mode | Processus | Surface |
|---|---|---|
| `api` | uvicorn (4 workers) | `/config`, `/enroll`, `/telemetry`, `/catalog`, `/relay`, `/binaries`, `/update` |
| `admin` | uvicorn (1 worker) | `/admin/*` (Jinja2 + HTMX + DSFR) |
| `worker` | `python -m app.worker_main` | Traitement de la queue de jobs |
| `all` | les trois combinés | Dev local uniquement |

En production K8s : 4 pods API + 1 pod admin + 2 pods worker. Toute la coordination inter-process
passe par **PostgreSQL** — pas de Redis, pas de broker. Justification et critique détaillées :
[adr-product-architecture §2.1–2.2](adr-product-architecture.md).

### 1.2 Backend unique PostgreSQL

PostgreSQL 16 assure trois rôles : base de données, **queue de jobs** (`queue_jobs` avec
`FOR UPDATE SKIP LOCKED`), et **stockage de télémétrie**. Une seule dépendance d'infrastructure à
opérer. Voir [adr-product-architecture §2.2](adr-product-architecture.md) pour les limites de scalabilité.

### 1.3 Administration server-rendered

L'admin UI est rendue côté serveur (Jinja2 + HTMX + DSFR), **sans build frontend** (pas de npm/webpack).
Voir [adr-product-architecture §2.5](adr-product-architecture.md).

---

## 2. Choix d'architecture structurants

| # | Décision | Justification courte | Détail |
|---|---|---|---|
| A1 | Monolithe FastAPI multi-modal | Code partagé, une image, pas de sync inter-services | [§2.1](adr-product-architecture.md) |
| A2 | PostgreSQL backend unique | Une seule dépendance à opérer, ACID, JSONB, SKIP LOCKED | [§2.2](adr-product-architecture.md) |
| A3 | Pipeline de config en 10 étapes | Les plugins n'ont aucune URL/secret hardcodé | [§2.3](adr-product-architecture.md) |
| A4 | Relay authentifié (nginx) | Les secrets upstream ne quittent jamais le serveur | [§2.4](adr-product-architecture.md) |
| A5 | Admin Jinja2 + HTMX (zéro build JS) | Pas de tooling frontend, conformité DSFR | [§2.5](adr-product-architecture.md) |
| A6 | Auth multi-couche (OIDC/PKCE/JWT/HMAC) | Un threat model par surface | [§2.6](adr-product-architecture.md) + §4 ci-dessous |
| A7 | Déploiement Kustomize multi-profils | Natif kubectl, overlays additifs (local/scaleway/dgx) | [§2.7](adr-product-architecture.md) |
| A8 | Catalogue + déploiement progressif | Canary→100 % au cœur du produit, pas un add-on | [§2.8](adr-product-architecture.md) + §3 ci-dessous |
| A9 | Distribution binaires (local/presign/proxy) | Pas de NFS partagé, S3 décharge le backend | [§2.9](adr-product-architecture.md) |
| A10 | Télémétrie OTLP intégrée | Visibilité usage réel, compatible SDK OpenTelemetry | [§2.10](adr-product-architecture.md) |

**Stack résumée** : Python 3.12 / FastAPI / PostgreSQL 16 / Jinja2+HTMX+DSFR / nginx 1.27 / Docker
multi-arch / Kubernetes + Kustomize. Tableau complet : [adr-product-architecture §6](adr-product-architecture.md).

---

## 3. Modèle de distribution de valeur vers le plugin

C'est le cœur fonctionnel de DM : **comment un plugin installé reçoit sa configuration, ses mises à
jour, ses binaires et son accès aux services**, sans embarquer ni URL ni secret.

```
                 ┌─────────────────────────── device-management ───────────────────────────┐
   Plugin ──(1)──▶ GET /config/{device}/config.json   (pipeline 10 étapes, cache 60s)
          ◀────── config dynamique : URLs, flags, campagne d'update, communications
          ──(2)──▶ POST /enroll        (Bearer PKCE Keycloak)  → relay_client_id + relay_key
          ──(3)──▶ /relay/* via nginx  (relay_key)  → Keycloak / LLM / API externes
          ──(4)──▶ GET /binaries/{path}             → presign S3 (302) | proxy stream | local
          ──(5)──▶ POST /telemetry/v1/traces (JWT télémétrie 300s) → PostgreSQL → upstream OTLP
          ──(6)──▶ POST /update/status  (auth relay) → pilote l'avancement de campagne
                 └───────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Configuration comme service (canal de valeur principal)
La route `GET /config/{device}/config.json` assemble dynamiquement la config via un **pipeline de
10 étapes** (résolution device → template → injection champs → substitution variables → overrides DM
→ overrides catalog par profil → clients Keycloak → contrôle d'accès → masquage secrets → enrichissement
campagnes/flags/communications). Le plugin reçoit tout ce dont il a besoin **sans rien savoir de
l'infrastructure**. Détail : [adr-product-architecture §2.3](adr-product-architecture.md).

### 3.2 Déploiement progressif (campagnes)
Le catalogue porte le cycle de vie des versions et des **campagnes** : `Draft → Active → (Paused) →
Completed | Rolled Back`, avec ciblage par pourcentage (canary 5→25→100 %), pattern email, groupe
Keycloak ou cohorte manuelle. La directive d'update est injectée dans le `config.json` (champ `update`)
quand une campagne active cible une version supérieure à `X-Plugin-Version`. Le plugin reporte son
avancement via `POST /update/status`. Détail : [adr-product-architecture §2.8](adr-product-architecture.md)
et le guide [plugin-developer/plugin-dm-protocol-update-features](../plugin-developer/plugin-dm-protocol-update-features.md).

### 3.3 Distribution de binaires
Trois modes (`local`, `presign` S3, `proxy`) avec **pull-on-miss** : les pods API sans PVC tirent le
binaire depuis le pod admin au premier accès. Détail : [adr-product-architecture §2.9](adr-product-architecture.md).

### 3.4 Télémétrie
Ingestion OTLP → stockage PostgreSQL → relai upstream, sous **jeton télémétrie distinct** (JWT signé,
TTL 300 s). Détail : [adr-product-architecture §2.10](adr-product-architecture.md).

> Le détail d'intégration côté plugin (PKCE, endpoints, cURL, packaging, protocole update) est dans
> l'espace **développeur de plugin** : [../plugin-developer/](../plugin-developer/).

---

## 4. Modèle de sécurité

### 4.1 Authentification multi-surface
Chaque surface d'accès a son propre mécanisme, calibré sur son threat model :

| Surface | Mécanisme | Stockage |
|---|---|---|
| Plugin → config | Aucun (public, secrets masqués) | — |
| Plugin → enroll | Bearer PKCE (Keycloak) | JWKS cache |
| Plugin → relay | `relay_client_id` + `relay_key` | hash SHA256 + pepper (DB), TTL 30 j |
| Plugin → télémétrie | JWT signé (DM) | clé symétrique |
| Admin UI → dashboard | OIDC Authorization Code + PKCE | cookie HMAC |
| Tool/machine → vault | client_credentials | DB |

Principe directeur : **les plugins ne peuvent pas garder de secret de longue durée** (une extension
LibreOffice est un fichier lisible) — d'où le relay, qui garde les secrets upstream côté serveur et
permet une **révocation atomique**. Détail et critique : [adr-product-architecture §2.6](adr-product-architecture.md).

### 4.2 Boot gate fail-closed
Au démarrage, `validate_security_config` (`app/main.py`) **refuse de démarrer** en
prod/staging/production si une des conditions de sécurité n'est pas remplie :
- `ADMIN_SESSION_SECRET` ou `DM_RELAY_SECRET_PEPPER` laissés à leur valeur par défaut,
- `DM_ALLOW_ORIGINS` valant `*` ou vide,
- `DM_DEV_AUTOLOGIN` actif.

Le comportement est **fail-closed** : un secret oublié provoque un `CrashLoopBackOff` explicite
(`Refusing to start`) plutôt qu'un déploiement silencieusement vulnérable. En dev, les défauts sont
tolérés. Origine : remédiation d'audit IMM-1 — voir [../security/audit-remediation-report](../security/audit-remediation-report.md).

### 4.3 Callback OIDC admin
Le redirect admin est **dérivé de `origin(PUBLIC_BASE_URL) + /admin/callback`** (client Keycloak
`bootstrap-iassistant` public/PKCE, secret vestigial vidé). L'ID Token est vérifié comme un JWS via
`PyJWKClient`, le JWKS étant récupéré par l'URL interne. Un cookie CSRF (`dm_csrf_token`) est posé
dans le callback.

### 4.4 Doctrine des secrets
- Les secrets **ne quittent jamais le serveur** (modèle relay, A4).
- **Masquage consolidé** : `SENSITIVE_ENV_VARS` + `is_sensitive_key()` / `mask_secret()` — un secret
  n'apparaît jamais dans une config servie sans en-têtes relay (étape 9 du pipeline).
- Les secrets sont **hors du repo** : stockés dans `~/.dm-secrets/` (DGX, persistant entre redeploys),
  patchés par overlay Kustomize, avec un horizon External Secrets / vault Cloud Pi Native.
- **Rotation** : credentials relay TTL 30 j renouvelés à chaque enrôlement.

> Le périmètre auditeur (constats, conformité référentielle, points de vigilance, doctrine) est
> regroupé dans l'espace **sécurité** : [../security/](../security/).

### 4.5 Chaîne d'approvisionnement (mesures organisationnelles)
Au-delà des mesures techniques (empreinte/checksum des artefacts, `pip-audit`), la **gestion du cycle de
déploiement** — publication d'une version, activation/pause/retour arrière d'une campagne, mise en ligne
d'un artefact — est **soumise à autorisation** : OIDC + **groupe d'administration** requis + **audit**
horodaté (`admin_audit_log`). Les **politiques d'autorisation et d'exception** (éligibilité des postes)
relèvent du **gestionnaire de parc**, en amont. *Roadmap* (après stabilisation) : **scan de sécurité des
packages** (anti-malware, signature, analyse par inférence du contenu), **distribution par plaque**
(topologie/charge réseau, anti-saturation lors d'un déploiement massif type *hotfix*), et **DM bi-site**
avec réplication bas niveau (les plugins portent 2 URLs → résilience sans composant réseau central).

---

## 5. Conséquences

**Positives** : simplicité opérationnelle (une image, un schéma SQL, un process/mode) ; zéro build
frontend ; secrets jamais exposés ; déploiement progressif natif ; config comme service (aucune URL
hardcodée côté plugin) ; audit intégré ; multi-environnement sans duplication.

**Dette / négatif** : `main.py` et `admin/router.py` trop volumineux ; télémétrie en PostgreSQL peu
scalable ; pas de rate limiting ; relay = point de défaillance unique. Tableau de dette et roadmap
détaillés : [adr-product-architecture §4–5](adr-product-architecture.md).

---

## Références

| Sujet | Document |
|---|---|
| Architecture produit détaillée (décisions §2.1–2.10) | [adr-product-architecture.md](adr-product-architecture.md) |
| Déploiement on-premise DGX | [adr-dgx-deployment.md](adr-dgx-deployment.md) |
| Intégration plugin (PKCE, endpoints, packaging, protocole) | [../plugin-developer/](../plugin-developer/) |
| Remédiation d'audit de sécurité | [../security/audit-remediation-report.md](../security/audit-remediation-report.md) |
