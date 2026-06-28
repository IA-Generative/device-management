# ADR : Architecture produit device-management

**Date** : 2026-04-11
**Statut** : En vigueur
**Auteurs** : <USERNAME> + Claude Opus 4.6

---

## 1. Qu'est-ce que device-management ?

Device-management est une **plateforme de gestion du cycle de vie de plugins bureautiques** (LibreOffice, Thunderbird, Firefox, Chrome/Edge). Il ne s'agit pas d'un app store : le systeme gere tout ce qui se passe *apres* l'installation — configuration, mises a jour progressives, telemetrie, communications, acces aux API externes.

**Fonctions principales** :
- Distribution de configuration dynamique (par profil, par environnement)
- Enrolement de postes et provisionnement d'identites
- Deploiement progressif (canary → 100%) avec campagnes
- Proxy authentifie (relay) vers Keycloak, LLM, telemetrie
- Catalogue public de plugins (vitrine DSFR + API JSON)
- Administration web (dashboard, CRUD catalog, campagnes, communications)
- Ingestion et relai de telemetrie (OTLP)
- Distribution de binaires (artefacts versionnes)

---

## 2. Decisions architecturales

### 2.1 Monolithe FastAPI multi-modal

**Decision** : une seule base de code Python (FastAPI), 4 modes d'execution via `DM_RUNTIME_MODE`.

| Mode | Processus | Endpoints |
|---|---|---|
| `api` | uvicorn (4 workers) | /config, /enroll, /telemetry, /catalog, /relay |
| `admin` | uvicorn (1 worker) | /admin/* (Jinja2 + HTMX) |
| `worker` | python -m app.worker_main | Traitement de la queue jobs |
| `all` | les trois combines | Dev local uniquement |

En production K8s : 4 pods API + 1 pod admin + 2 pods worker.

**Justification** :
- Code partage entre les modes (modele de donnees, services, utilitaires)
- Un seul Dockerfile, une seule image Docker
- Pas de synchronisation inter-services (tout passe par PostgreSQL)
- Le mode `all` permet un cycle de dev rapide (un seul process)

**Critique** :
- `app/main.py` fait 4244 lignes — trop gros pour un monolithe sain
- `app/admin/router.py` fait 3486 lignes — devrait etre decoupe en sous-modules
- Pas de separation claire entre couche HTTP et logique metier dans main.py (les endpoints font du SQL direct)
- Le mode `admin` charge tout le code API inutilement

**Amelioration proposee** :
- Extraire les services metier de main.py vers `app/services/` (config.py, enrollment.py, relay.py, telemetry.py, catalog.py)
- Decouper admin/router.py par domaine fonctionnel (catalog_routes.py, campaign_routes.py, device_routes.py)
- Lazy import des modules admin/worker selon le runtime mode

---

### 2.2 PostgreSQL comme backend unique (DB + queue + telemetrie)

**Decision** : PostgreSQL 16 sert de base de donnees, de queue de jobs, et de stockage de telemetrie. Pas de Redis, pas de RabbitMQ, pas de ClickHouse.

**Schema** : 438 lignes, 20+ tables, fichier unique `db/schema.sql`, pas de migrations versionnees.

**Queue** : table `queue_jobs` avec `FOR UPDATE SKIP LOCKED` pour le claim, backoff exponentiel, dead letter.

**Justification** :
- Une seule dependance d'infrastructure a operer
- PostgreSQL SKIP LOCKED est suffisant pour < 100 workers
- JSONB pour les donnees flexibles (config_template, payload, rollout_config)
- ACID garanti sur les transactions d'enrolement
- Pas de latence reseau inter-services

**Critique** :
- Pas de migrations versionnees (Alembic, Flyway) → risque de drift entre schema.sql et la DB reelle
- Le schema utilise `IF NOT EXISTS` partout — masque les erreurs de migration
- La telemetrie dans PostgreSQL ne scale pas (chaque span = un INSERT)
- Le polling de queue (500ms) consomme des connexions DB meme a vide
- Pas de connection pooling externe (PgBouncer) — le pool est in-process (2-10 connexions)

**Amelioration proposee** :
- Introduire Alembic pour les migrations versionnees
- Telemetrie : envisager un stockage dedie (ClickHouse, ou simplement forwarding sans persistence locale)
- Queue : si le volume depasse 1000 jobs/min, envisager un broker (Redis Streams, NATS)
- Ajouter PgBouncer en sidecar pour le connection pooling en production

---

### 2.3 Pipeline de configuration en 10 etapes

**Decision** : la route `GET /config/{device_name}/config.json` execute un pipeline de 10 etapes pour assembler la config finale.

```
1. Resolution device (slug ou alias → plugin_id)
2. Chargement template (DB ou fichier)
3. Injection champs obligatoires (device_name, config_path, bootstrap_url)
4. Substitution variables (${{LLM_BASE_URL}} → valeur env)
5. Injection overrides DM (telemetrie, relay)
6. Overrides catalog (plugin_env_overrides par profil)
7. Injection clients Keycloak (par environnement)
8. Controle d'acces (maturite, mode d'acces, waitlist)
9. Masquage secrets (si pas de relay headers)
10. Enrichissement (campagnes, feature flags, communications)
```

Cache en memoire : TTL 60 secondes, invalide sur mutation DB.

**Justification** :
- Les plugins n'ont pas a connaitre les URLs d'infrastructure
- La config est assemblee dynamiquement selon le profil (dev/int/prod)
- Le masquage des secrets force l'enrolement (pas de credentials sans auth)
- Le cache evite les requetes DB a chaque appel plugin

**Critique** :
- 10 etapes = complexe a debugger quand la config n'est pas celle attendue
- Pas de dry-run pour previsualiser la config finale d'un device sans l'appeler
- Le cache de 60s peut causer des configurations obsoletes en cas de rollout rapide
- La substitution de variables est un mini-moteur de template ad-hoc (regex `${{VAR}}`) qui ne gere pas les valeurs par defaut ni les conditions

**Amelioration proposee** :
- Ajouter un endpoint `/admin/api/config-preview?device=X&profile=Y` pour dry-run
- Documenter les 10 etapes dans un diagramme (actuellement implicite dans le code)
- Ajouter un header `X-Config-Cache-Hit: true/false` pour le debugging
- Envisager un format de template plus puissant si les besoins de conditionnalite augmentent

---

### 2.4 Systeme de relay authentifie (relay-assistant)

**Decision** : les plugins ne contactent jamais directement Keycloak, le LLM, ou les API externes. Tout passe par un proxy nginx (`relay-assistant`) authentifie par des credentials de relay.

```
Plugin → relay-assistant (nginx) → auth_request → device-management /relay/authorize
                                 → proxy_pass → upstream (Keycloak, LLM, etc.)
```

**Credentials de relay** :
- `relay_client_id` (public) + `relay_key` (secret)
- Hash SHA256 avec pepper cote serveur
- TTL 30 jours, rotation a chaque enrolement
- Verification par device-management sur chaque requete relay

**Justification** :
- Les secrets API (tokens LLM, credentials Keycloak) restent cote serveur
- Revocation atomique (supprimer le relay_client → plus d'acces)
- Audit centralise (chaque acces relay est trace)
- Les plugins ne voient jamais les tokens upstream

**Critique** :
- Point de defaillance unique : si relay-assistant tombe, tous les plugins perdent l'acces
- Pas de circuit breaker : si un upstream est lent, le relay bloque
- Le relay nginx n'a pas de rate limiting
- Les credentials de relay expirent en 30 jours — si l'utilisateur ne re-enrole pas, tout s'arrete silencieusement

**Amelioration proposee** :
- Ajouter un health check par upstream dans le relay (circuit breaker natif nginx ou via Lua)
- Rate limiting par relay_client_id dans nginx
- Notification proactive avant expiration des credentials (via le endpoint config)
- HPA sur relay-assistant pour la scalabilite horizontale

---

### 2.5 Admin UI sans framework JS (Jinja2 + HTMX + DSFR)

**Decision** : l'interface d'administration est rendue cote serveur (Jinja2) avec HTMX pour l'interactivite. Pas de React, pas de Vue, pas de build frontend.

**Justification** :
- Zero build tooling (pas de webpack, vite, npm)
- L'UI est dans le meme repo que le backend — pas de deploy frontend separe
- HTMX permet les interactions dynamiques (tabs, formulaires partiels, loading states)
- DSFR (CDN) pour la conformite design de l'Etat
- Temps de chargement initial tres rapide (HTML pur, pas de JS bundle)

**Critique** :
- Les templates Jinja2 de 800+ lignes (catalog_plugin.html) deviennent difficiles a maintenir
- Pas de tests frontend automatises
- Le JavaScript inline (non-module) est fragile et non-type
- Certains formulaires necessitent du JS complexe (upload + analyse + preview) qui serait plus propre en composants
- Le WAF du DGX bloque les form submit natifs → necessitant un bypass global `fetch()` dans base.html

**Amelioration proposee** :
- Decomposer les templates en composants Jinja2 reutilisables (`{% include %}` ou macros)
- Ajouter des tests E2E avec Playwright (headless browser)
- Envisager un hybride : Jinja2 pour le layout, Alpine.js ou Petite-Vue pour les composants interactifs complexes
- Extraire le JavaScript inline dans des fichiers `.js` statiques (cacheable, lintable)

---

### 2.6 Authentification multi-couche (OIDC, PKCE, JWT, HMAC)

**Decision** : chaque surface d'acces a son propre mecanisme d'authentification.

| Surface | Mecanisme | Stockage |
|---|---|---|
| Plugin → config | Aucun (public) | — |
| Plugin → enroll | Bearer PKCE (Keycloak) | JWKS cache |
| Plugin → relay | relay_client_id + relay_key | DB hash |
| Plugin → telemetrie | JWT signe (DM) | Cle symetrique |
| Admin UI → dashboard | OIDC Authorization Code + PKCE | Cookie HMAC |
| Tool/machine → vault | client_credentials | DB |

**Justification** :
- Chaque surface a un threat model different
- Les plugins ne peuvent pas stocker des secrets de longue duree (LibreOffice extension = fichier accessible)
- Le relay ajoute une couche d'indirection qui protege les secrets upstream
- L'admin UI utilise des standards (OIDC + PKCE) pour le SSO

**Critique** :
- 4 mecanismes d'auth differents = complexite operationnelle et cognitive
- Le CSRF token n'est pose que en mode dev (pas en OIDC) — la protection CSRF est techniquement absente en production (mitige par SameSite=lax)
- Le `ADMIN_SESSION_SECRET` par defaut est `changeme-dev-only` — si oublie en prod, tout le monde est admin
- Les tokens telemetrie (300s TTL) sont trop courts pour des plugins offline intermittents
- Pas de refresh token pour le relay (re-enrollment complet necessaire)

**Amelioration proposee** :
- Poser le CSRF cookie dans le callback OIDC (pas seulement en dev mode)
- Forcer une erreur au demarrage si `ADMIN_SESSION_SECRET == changeme-dev-only` en mode prod
- Ajouter un mecanisme de refresh pour les relay credentials sans re-enrollment complet
- Documenter le threat model pour chaque surface d'authentification

---

### 2.7 Deploiement via Kustomize multi-profils

**Decision** : une base Kustomize unique + 3 overlays (local, scaleway, dgx). Pas de Helm.

**Justification** :
- Kustomize est natif a kubectl (pas de dependance supplementaire)
- Les overlays sont additifs — la base ne change jamais pour un profil specifique
- Strategic merge patches pour les differences (proxy, secrets, images)
- Plus lisible que des templates Helm pour une equipe petite

**Critique** :
- Pas de templating conditionnel (if/else) — certaines configurations necessitent des fichiers de patch separes (4 fichiers proxy-patch)
- Pas de `helm diff` pour previsualiser les changements
- Le rollback est manuel (`kubectl rollout undo`) — pas de `helm rollback`
- Les secrets sont dans le repo (meme si patches par overlay)
- Le nombre de fichiers dans l'overlay DGX a explose (9 fichiers pour un seul profil)

**Amelioration proposee** :
- Migrer vers Helm si le nombre de profils depasse 4 (les values.yaml deviennent plus maintenables)
- Utiliser ArgoCD pour le deploiement continu (prevu, voir prompt-migration-argocd.md)
- Sealed Secrets ou External Secrets Operator pour sortir les secrets du repo

---

### 2.8 Catalogue de plugins avec deploiement progressif

**Decision** : le catalogue combine CRUD de plugins, gestion de versions, et campagnes de deploiement (canary/immediat) avec ciblage par cohortes.

**Campagne** :
```
Draft → Active → (Paused) → Completed | Rolled Back
```

**Ciblage** :
- Tous les devices
- Pourcentage (canary 5% → 25% → 100%)
- Pattern email (regex)
- Groupe Keycloak
- Cohorte manuelle (liste d'UUIDs)

**Justification** :
- Le deploiement progressif est critique pour des plugins bureautiques (1000+ postes)
- Le ciblage par cohorte permet les beta tests internes
- La campagne est un objet de premiere classe avec son propre cycle de vie

**Critique** :
- Pas de rollback automatique si X% des devices echouent
- Pas de prevention des campagnes en double pour le meme plugin
- La comparaison de versions est textuelle (pas de semver parsing)
- Le device doit reporter son status (`/update/status`) — si le plugin ne le fait pas, la campagne reste bloquee
- Pas de deadline automatique sur les campagnes (elles restent actives indefiniment)

**Amelioration proposee** :
- Ajouter un seuil de rollback automatique (`rollback_on_failure_threshold: 10%`)
- Parser les versions en semver pour permettre les ranges (`>=2.0.0, <3.0.0`)
- Ajouter une deadline aux campagnes (auto-complete apres N jours)
- Constraint SQL pour empecher 2 campagnes actives sur le meme plugin

---

### 2.9 Distribution de binaires (local + S3 + pull-on-miss)

**Decision** : 3 modes de distribution des artefacts binaires (extensions .oxt, .xpi, .crx).

| Mode | Fonctionnement | Usage |
|---|---|---|
| `local` | Fichiers sur disque, servis par FastAPI | Dev, petit volume |
| `presign` | URL presignee S3 (302 redirect, 5min TTL) | Production (Scaleway Object Storage) |
| `proxy` | Stream depuis S3 via le pod | Fallback si presign non supporte |

**Pull-on-miss** : les pods API (4 replicas) n'ont pas de PVC propre. A la premiere demande d'un binaire, le pod le tire depuis le pod admin (qui a le PVC) via HTTP interne.

**Justification** :
- Pas besoin de NFS partage (complexe en K8s multi-node)
- S3 presign decharge le backend (le client download directement depuis S3)
- Pull-on-miss evite de synchroniser 4 PVCs

**Critique** :
- Le premier acces a un binaire est lent (double transfert : admin → api → client)
- Si le pod admin est down, les binaires ne sont plus accessibles
- Pas de CDN devant le S3 (chaque download = une requete S3)
- Les checksums sont stockes mais pas verifies cote client (pas de signature)

**Amelioration proposee** :
- Ajouter un CDN (CloudFront/Cloudflare) devant le S3 pour le caching
- Signer les binaires (GPG ou sigstore) pour verification cote plugin
- Pre-pousser les binaires sur tous les API pods au moment du `kubectl apply` (init container)

---

### 2.10 Telemetrie integree (OTLP → PostgreSQL → upstream)

**Decision** : device-management ingere les spans OpenTelemetry des plugins, les stocke en DB, et les relaie vers un collecteur upstream.

**Justification** :
- Visibilite sur l'usage reel des plugins (quelles fonctionnalites, quels erreurs)
- Le stockage local permet l'analyse meme si l'upstream est down
- Le token telemetrie (JWT 300s) est separe des credentials de relay
- Compatible OTLP standard — les plugins utilisent le SDK OpenTelemetry natif

**Critique** :
- Stocker chaque span en PostgreSQL (INSERT par span) ne scale pas au-dela de quelques milliers de devices
- Le token de 300s est trop court pour des plugins qui restent ouverts des heures (il est renouvele a chaque fetch config, mais si le plugin ne refetch pas...)
- Pas d'aggregation cote serveur (chaque span brut est stocke)
- La table `device_telemetry_events` n'a pas de politique de retention

**Amelioration proposee** :
- Retention automatique (DELETE WHERE created_at < now() - interval '30 days')
- Aggregation : stocker des metriques resumees au lieu des spans bruts
- Envisager de bypasser le stockage local et forwarding direct vers un backend dedie (Grafana Tempo, Jaeger)
- Allonger le TTL du token telemetrie (1h au lieu de 5min)

---

## 3. Points forts de l'architecture

1. **Simplicite operationnelle** : une image, un schema SQL, un process par mode
2. **Zero build frontend** : l'admin UI fonctionne sans npm, webpack, ni node_modules
3. **Securite par design** : les secrets ne quittent jamais le serveur (relay)
4. **Deploiement progressif natif** : pas un add-on, c'est au coeur du produit
5. **Config comme service** : les plugins n'ont aucune URL hardcodee
6. **Audit integre** : chaque action admin, enrolement, et acces relay est trace
7. **Multi-environnement** : un seul repo, 3 overlays, zero duplication

---

## 4. Dette technique identifiee

| Priorite | Element | Impact | Effort | Statut |
|---|---|---|---|---|
| **Haute** | main.py = 4244 lignes | Maintenance difficile, merge conflicts | 2-3 jours | 🟡 Commence — `app/services/db.py` et `app/services/crypto.py` extraits (4244 → 4040 lignes, -5%). Le reste (relay, config pipeline, enrollment, telemetry) est fortement couple a FastAPI et necessite un refactoring d'interfaces. |
| **Haute** | Pas de migrations DB versionnees | Risque de drift schema, pas de rollback DB | 1 jour | ✅ **Resolu** — Alembic installe, migration initiale `001` wrapping `db/schema.sql`. Cycle upgrade/downgrade/upgrade valide sur PostgreSQL 16. Commit `ae17ade`. |
| **Haute** | CSRF absent en prod (cookie pose uniquement en dev) | Vulnerabilite CSRF sur les POST admin | 0.5 jour | ✅ **Resolu** — Cookie `dm_csrf_token` pose dans le callback OIDC. Warning CRITICAL au demarrage si `SESSION_SECRET` est le defaut en prod. Commit `0b33f29`. |
| **Moyenne** | Secrets dans le repo (all-secrets.yaml) | Risque de fuite | — | ✅ **Resolu** — Secrets exclus du manifest pre-rendu, stockes dans `~/.dm-secrets/` (persistant entre redeploys). Commit `b012158`. |
| **Moyenne** | Pas de tests automatises | Regression non detectee | 3-5 jours | 🟡 Partiel — 68 tests unitaires existants passent, 14 tests E2E WAF+Envoy (`10-e2e-waf-test.sh`). Manque : tests d'enrollment, config pipeline, relay auth. |
| **Moyenne** | JavaScript inline non-type dans les templates | Bugs silencieux | 1-2 jours | Non traite |
| **Basse** | Telemetrie en PostgreSQL | Scalabilite limitee | Selon volume reel | Non traite |
| **Basse** | Pas de rate limiting | DoS possible | 0.5 jour | Non traite |
| **Basse** | Pas de pagination sur les endpoints de liste | Lent avec > 1000 entries | 1 jour | Non traite |

---

## 5. Options d'amelioration (roadmap)

### Court terme (< 1 mois)

1. **Decouper main.py** : extraire les services metier dans `app/services/`
   - ✅ `app/services/db.py` : pool de connexions, bootstrap schema, helpers URL (268 lignes)
   - ✅ `app/services/crypto.py` : base64url, hash relay, tokens telemetrie (95 lignes)
   - 🔲 Reste a faire : relay, config pipeline, enrollment, telemetry spans (~1500 lignes)
   - Bloqueur : ces fonctions dependent de `FastAPI.HTTPException` et `Request` — necessite un refactoring d'injection de dependances
   - main.py passe de 4244 a 4040 lignes (-204, ~5%). Objectif : < 2000 lignes.

2. ✅ **Alembic installe** — commit `ae17ade`
   - `alembic.ini`, `alembic/env.py`, `alembic/versions/001_initial_schema.py`
   - Migration initiale wrap `db/schema.sql` (idempotent, IF NOT EXISTS)
   - Cycle upgrade/downgrade/upgrade valide sur PostgreSQL 16
   - Futures evolutions de schema = nouveaux fichiers de migration

3. ✅ **CSRF fixe en production** — commit `0b33f29`
   - Cookie `dm_csrf_token` pose dans le callback OIDC (`app/admin/router.py`)
   - Warning CRITICAL si `ADMIN_SESSION_SECRET == "changeme-dev-only"` en prod (`app/admin/auth.py`)
   - Nettoyage du cookie `dm_pkce_verifier` apres le callback

4. **Tests E2E** : ✅ 14 tests WAF+Envoy dans `10-e2e-waf-test.sh`
   - 🔲 Reste a faire : tests enrollment, config distribution, relay authorize, telemetry ingestion

### Moyen terme (1-3 mois)

5. **ArgoCD + GitLab CI** : automatiser le build/deploy (voir prompt-migration-argocd.md)
   - GitLab Runner build l'image
   - ArgoCD sync les manifests
   - Plus de deploiement manuel

6. **External Secrets Operator** : sortir les secrets de git definitivement
   - Vault/OpenBao pour le stockage
   - ESO pour la synchronisation vers K8s

7. **Helm chart** : si le nombre d'environnements depasse 3-4
   - Un values.yaml par environnement
   - `helm diff` pour les previews
   - `helm rollback` pour la securite

8. **Observabilite** : dashboard Grafana pour les metriques cles
   - Enrolements/jour
   - Taux d'erreur relay
   - Latence config P95
   - Campagnes en cours

### Long terme (3-6 mois)

9. **MyVault** : coffre-fort de credentials utilisateur (voir prompt_myvault_v2.md)
   - Les plugins stockent leurs credentials utilisateur dans un vault centralise
   - SDK Python pour l'integration OpenWebUI

10. **Federation multi-cluster** : synchronisation catalog/config entre Scaleway et DGX
    - Un cluster maitre (Scaleway) qui pousse vers les replicas (DGX)
    - Gestion des deltas (config differente par site)

11. **Marketplace** : ouvrir le catalogue a des editeurs tiers
    - Workflow de soumission/review
    - Signatures de packages
    - SLA par editeur

### Securisation supply chain & resilience (apres stabilisation)

12. **Scan de securite des packages** : analyser les artefacts uploades avant publication (anti-malware,
    verification de signature/integrite), complete par une **analyse du contenu par inference**. Reduit le
    risque LLM01 (la generation de fiches consomme du contenu de paquet non maitrise) et le risque
    d'artefact malveillant.

13. **Distribution par plaque** : distribuer les binaires selon la topologie et la charge reseau
    (miroirs / plaques regionales) pour eviter la saturation d'un point unique lors d'un deploiement massif
    (ex. hotfix de securite pousse simultanement a toute la flotte).

14. **DM bi-site avec failover cote client** : deployer DM sur 2 sites avec replication bas niveau
    (cluster CloudNativePG replique bi-site pour la base) ; les plugins portent **2 URLs de DM** et
    basculent de l'une a l'autre. Resilience assuree **sans composant reseau central** (pas de repartiteur
    ni de bascule centralisee a operer).

### Mesures organisationnelles de supply chain (en place)

- **Cycle de deploiement soumis a autorisation** : publication de version, activation/pause/rollback de
  campagne et mise en ligne d'artefact passent par l'IHM admin (OIDC + groupe requis) et sont traces
  (`admin_audit_log`) — mesure de securite de la chaine d'approvisionnement.
- **Politiques d'autorisation et d'exception** : a la main du gestionnaire de parc (WAPT / SCCM / Intune),
  en amont de DM.

---

## 6. Stack technique resumee

| Couche | Technologie | Justification |
|---|---|---|
| Runtime | Python 3.12 | Ecosysteme riche, FastAPI performant |
| API | FastAPI | Async, OpenAPI auto, Pydantic validation |
| DB | PostgreSQL 16 | JSONB, SKIP LOCKED, ACID, extensible |
| Queue | PostgreSQL (custom) | Zero dependance supplementaire |
| Templates | Jinja2 | Natif FastAPI, pas de build |
| Frontend | HTMX + DSFR | Zero JS build, conformite Etat |
| Auth | OIDC / JWT / HMAC | Standards, Keycloak compatible |
| Proxy | nginx 1.27 | Relay vers upstreams, auth_request |
| Container | Docker (multi-arch) | amd64 + arm64 |
| Orchestration | Kubernetes + Kustomize | Natif kubectl, multi-profils |
| CI/CD | Manuel (scripts) | A migrer vers GitLab CI + ArgoCD |

---

## References

| Document | Chemin |
|---|---|
| Code principal | `app/main.py` (4040 lignes, was 4244) |
| Service DB | `app/services/db.py` (268 lignes, extrait de main.py) |
| Service Crypto | `app/services/crypto.py` (95 lignes, extrait de main.py) |
| Admin UI | `app/admin/router.py` (3486 lignes) |
| Admin Auth | `app/admin/auth.py` (OIDC, session, CSRF) |
| Schema DB | `db/schema.sql` (438 lignes) |
| Alembic migrations | `alembic/versions/001_initial_schema.py` |
| Settings | `app/settings.py` (153 lignes, 155+ env vars) |
| Queue | `app/postgres_queue.py` |
| Dockerfile | `deploy/docker/Dockerfile` |
| Kustomize base | `deploy/k8s/base/` |
| Overlay DGX | `deploy/k8s/overlays/dgx/` |
| ADR deploiement DGX | `architecture/adr-dgx-deployment.md` |
| Test E2E WAF | `scripts/dgx-deploy/10-e2e-waf-test.sh` |
| Deploiement DGX | `scripts/dgx-deploy/dumb-deploy.sh` |
| Test connectivite | `scripts/dgx-deploy/09-connectivity-test.sh` |
| Migration ArgoCD | `prompts/prompt-migration-argocd.md` |
| MyVault (futur) | `prompts/prompt_myvault_v2.md` (externe) |
