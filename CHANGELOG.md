# Changelog

Historique reconstitué le 2026-09-28 depuis les commits du dépôt : une section par montée du fichier
`VERSION`, commits groupés par type. Les versions suivantes seront tenues par release-please.
Format des titres : `## [X.Y.Z] - date` (lu par `scripts/version_json.py`, servi sur `/__version__`).

## [0.9.20] - 2026-09-26

_0.9.19 a été publiée depuis une branche de release (#40) ; ses changements sont repris ici._

### Features

* **admin :** choisir la version générale ; tests unitaires et Postgres (#40)
* **catalog :** version générale par plugin, servie par tous les canaux natifs (#40)
* **admin :** renseigner extension_id depuis la fiche plugin
* **catalog :** feed natif LibreOffice /catalog/{slug}/update.xml (#4)
* **communications :** add the client ack endpoint
* **config :** expose active communications to the requesting client
* **communications :** honor cohort and version targeting when resolving active items

### Bug Fixes

* **catalog :** corrections de la revue qualité de la version générale (#40)
* **catalog :** update.xml — servabilité jugée par Postgres, plus de repli sur Host
* **admin :** extension_id validé, libellé selon le type de plugin
* **communications :** ack refusé sur un brouillon, bornes de version fail-safe
* **catalog :** échapper les attributs de update.xml sans xml.sax (Bandit B406)
* **catalog :** tracer le repli de update.xml sur l'en-tête Host
* **catalog :** update.xml n'annonce qu'une version réellement servable
* **campaigns :** /update/status compte « deferred » en notified, pas en failed
* **communications :** drop the phantom target_bundle_id column from the insert

### Refactoring

* **versions :** un seul parseur de version, dans app/services/versions
* **catalog :** update.xml passe par _with_bootstrap_cursor (plus de boucle de connexion dupliquée)

### Documentation

* **plugin-developer :** version générale, update.xml?version=, manifestes (#40)
* **plugin-developer :** communications absent d'un access_denied ; 404 et pré-releases
* **catalog :** feed LibreOffice — renvoi corrigé, retrait d'une version, sémantique notified
* **plugin-developer :** contrat du feed natif LibreOffice et sémantique deferred
* **plugin-developer :** document the communications contract

### Tests

* **int :** non-régression INT et recette de la version générale (#40)
* **feed :** fixture `mod` sous monkeypatch au lieu d'un état global
* **feed :** échappement, réversibilité de l'URL et ordre des routes
* **feed :** neutraliser le pool de connexions dans les tests du feed LibreOffice

## [0.9.18] - 2026-09-06

### Features

* **admin :** read version and device type from dm-manifest.json first

### Bug Fixes

* **build :** la voie in-cluster passe aussi DM_IMAGE_TAG
* **deploy :** l'image déclare sa version, au lieu d'un littéral tenu à la main
* **binaries :** GET /binaries/{path} servait le cache sans le vérifier (#5)
* **catalog :** vérifier le checksum du cache disque avant de servir un binaire (#5)
* **tests :** UP037 — annotation sans guillemets, FauxBase est défini au-dessus
* **tests :** E741 — « l » est un nom ambigu, les lignes s'appellent « ligne »
* **parc :** le catalogue se résout par nom d'export ET slug, et une absence se voit
* **admin :** keep the id_token OUT of the session cookie
* **admin :** expired API polls must not mint OIDC state cookies
* **admin :** report version source correctly in extract-version
* **db :** make startup role/database/schema auto-creation opt-in, stop hardcoding the DB name
* corrige les points relevés par le contrôle qualité du lot « lisibilité »
* **queue :** la mise en lettre morte ne doit plus tuer le worker
* **telemetry :** lire les attributs OTLP typés à la persistance SQL

### Documentation

* rend lisible la cohabitation rollout général / branches d'expérimentation

### Tests

* **binaries :** verrouiller le second appelant, l'artefact de variante
* **issue5 :** la sonde de concordance mesure aussi les plugins sans campagne
* **issue5 :** banc de validation — local Docker, Kubernetes, et sonde client

## [0.9.17] - 2026-09-04

### Features

* **parc :** les noms nus du contrat (PARC_EXPORT_ENABLED/INTERVALLE_S) acceptés en repli
* **admin :** section debug « Export parc → suivi-beta » + export manuel
* **parc :** export des agrégats d'usage vers le bus de la bêta (delta 5 min + instantané)

### Bug Fixes

* **admin :** l'activité device lisait une colonne received_at qui n'a jamais existé

### Tests

* **parc :** verrouille le contrat d'export — mapping, empreinte, deltas, rejeu, 409, anti-fuite

## [0.9.16] - 2026-08-30

### Features

* **admin :** signaler combien de versions circulent réellement sur le parc
* **catalogue :** rendre la cohabitation lisible — précédence, page taguée, API

### Bug Fixes

* corrige les points relevés par le contrôle qualité du lot « lisibilité »
* **queue :** la mise en lettre morte ne doit plus tuer le worker
* **telemetry :** lire les attributs OTLP typés à la persistance SQL
* **catalogue :** statut experimental invisible, et « dernière version » lexicographique

### Documentation

* **readme :** référence la documentation développeur et retire un lien mort
* capitalise la lisibilité de la cohabitation (0.9.15)

### Tests

* **e2e :** vérifie que l'API publique dit la même chose que le HTML (section E)

## [0.9.15] - 2026-07-26

### Bug Fixes

* **ci :** débloque le job qualité — finding Bandit B108 préexistant
* corrige les défauts relevés par le second contrôle qualité
* **campaigns :** épingle l'URL du bras d'expérimentation + factorise l'auto-complétion

### Tests

* **e2e :** ajoute la cohabitation étendue de plusieurs branches (section D)

### Build

* **k8s :** remplace Kaniko par BuildKit rootless
* **k8s :** fiabilise le job Kaniko (logs et disque déclaré)
* **k8s :** ajoute une option de build in-cluster avec Kaniko

## [0.9.14] - 2026-07-25

### Features

* **campaigns,catalog :** branches d'expérimentation — multi-versions par cohorte (0.9.14)
* **deploy :** chart Helm documenté pour device-management (api/admin/worker/telemetry-relay)
* **docker :** image non-root (uid 10001) + migrations Alembic embarquées
* **app :** prêt cloud-native — S3 sans PVC, observabilité, résilience, arrêt gracieux

### Bug Fixes

* **campaigns :** scope la campagne d'update au plugin du device demandeur (#14)
* **config :** lookup config_template déterministe — un doublon sans template ne masque plus le vrai
* **admin :** 403 générique sur le callback OIDC — ne révèle plus le nom du groupe admin requis
* **deploy :** runAsUser 70 explicite sur l'init wait-for-postgres — runAsNonRoot vérifie le User de l'image (vide = root kubelet) → CreateContainerConfigError

### Documentation

* **operations :** rattrape les apports 0.9.9→0.9.12 et les merges cloud-native/Helm

## [0.9.12] - 2026-07-14

### Features

* **admin :** histogramme du trafic LLM (chat vs embeddings) sur le dashboard
* **admin :** version du DM sur le tableau de bord (+ alerte versions mixtes) et modèle d'embedding sur la ligne LLM du debug

## [0.9.11] - 2026-07-14

### Features

* **flags :** feature flag tri-état (transparent / forcé ON / forcé OFF)

## [0.9.10] - 2026-07-14

### Bug Fixes

* **db+flags :** FK plugin_installations en ON DELETE CASCADE + réconciliation des flags sur les 2 derniers chemins d'import manqués

## [0.9.9] - 2026-07-14

### Bug Fixes

* **flags+telemetry :** réconciliation du catalogue sur TOUS les chemins d'import + telemetryEndpoint à la racine de l'origine

### Documentation

* spec fix pérenne telemetryEndpoint (double /bootstrap sur ingress à préfixe)
* ADR-0002 §5 (frontière validée : embeddings mutualisés, identité cuid) + ADR-0003 (choix modèle embedding) + mode opératoire feature flags + protocole features v2 (§4.4)

## [0.9.8] - 2026-07-14

### Bug Fixes

* **audit :** plugin:<id numérique> résolu via le catalogue, plugin:* sans plugin

## [0.9.7] - 2026-07-14

### Features

* **audit :** plugin_slug PERSISTÉ dans admin_audit_log — fix long terme

## [0.9.6] - 2026-07-14

### Features

* **audit :** colonne Plugin dérivée + filtre autocomplété

## [0.9.5] - 2026-07-14

### Features

* **audit :** journal dense — filtres live avec autocomplétion, période, recherche détails, scroll infini

## [0.9.4] - 2026-07-14

### Features

* **dashboard :** courbes par plugin + légende, tuiles cohérentes (appareils · interactions)
* **dashboard :** toggle Appareils/Utilisateurs sur le widget Adoption

### Bug Fixes

* **telemetry :** identité STABLE (cuid) dans le token — fin des pseudo-appareils par jti
* **installations :** version requise pour le heartbeat — pas d'installation fantôme sans version

## [0.9.3] - 2026-07-14

### Bug Fixes

* **admin+flags :** installations enfin enregistrées, création de flag scopée plugin+version, /admin/flags/{id} réparé

## [0.9.2] - 2026-07-13

### Features

* **build :** DM_REGISTRY_OVERRIDE + VERSION 0.9.2 (livraison int)
* **build :** build-k8s.sh sans argument → tag par défaut = VERSION
* **flags :** catalogue scopé par plugin + réconciliation à l'import + delete_flag
* **flags :** résolution serveur des feature flags — deep-merge template + cohortes seules
* **deploy :** wire EMBD_MODEL_NAME through manifests + docker (RAG embedder)
* **llm :** embedder via /llm/v1 — /embeddings passthrough + emit embd* in /config
* **deploy :** câble DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS (optional) sur le pod API — parcours prod-like/WAF via /auth/token
* **llm :** proxy LLM OpenAI-compatible /llm/v1 + override llmEndpoint (0.9.0)
* **routing :** / -> /catalog/ + /admin -> /admin/ (proxy-safe)
* **logs :** filtre aussi /admin/api/config/propagation (polling admin 5s)
* **logs :** filtre sondes — +/readyz, récap 15 min, fenêtre de grâce au démarrage + nginx access_log off
* **config :** légende + tooltips des statuts (modifié/redémarrage requis/secret) sur /admin/debug
* **config :** credentials éditables (Keycloak/relais) + bootstrap pré-import des overrides
* **config :** reaper auto des pods obsolètes + heartbeat résilient
* **config :** manifests k8s — Downward API (POD_IP/NODE_NAME), DM_CONFIG_SECRET_KEY, /readyz, version 0.8.0 (Stage 7)
* **config :** UI page debug — édition inline, diff, reset, recharger, éditeur ordonné, flotte santé (Stage 6)
* **config :** endpoints admin + endpoint interne + bootstrapUrls (Stage 4+5+8)
* **config :** câblage synchro runtime + garde de disponibilité (Stage 3+9)
* **config :** module cœur runtime_config (registre, baseline, résolution, reload, génération, enrôlement, poll+NOTIFY)
* **config :** helper santé par pod (RAM/load/cpu/requêtes), stdlib pur
* **config :** schéma des surcharges runtime (config_state, config_overrides, config_pod_state)
* **config :** chiffrement réversible Fernet pour les secrets de surcharge runtime

### Bug Fixes

* **telemetry :** preserve PUBLIC_BASE_URL path prefix in emitted endpoints (DGX /bootstrap 502)
* **deploy :** mapper RELAY_KEYCLOAK_UPSTREAM sur le pod API (relais token /auth/token répondait 503)
* **security :** image non-root (uid 10001) + garde-fou runAsNonRoot
* **deploy :** mapper DM_LLM_TOKEN_SIGNING_KEY sur le pod API (mint llmToken dans /config)
* **db :** verrou consultatif pg_advisory_lock autour de apply_schema
* **k8s :** telemetry-relay (image DM) — bloc runtime-config + /readyz (exemple public)
* **admin :** logout Keycloak — passe client_id (évite 'Missing parameters: id_token_hint')
* **admin :** corrige 2 NameError latents dans le router

### Documentation

* **llm :** document embeddings passthrough + embd* /config fields (RAG embedder)
* **adr :** ADR-0002 — réordonnancement pour lecture fluide (principe avant applications)
* **adr :** ADR-0002 contexte — sécabilité organisationnelle + découplage d'obsolescence
* **plugin :** §8bis — journalisation fonctionnelle des erreurs du relais LLM par les plugins
* **adr :** ADR-0002 — schéma des interfaces avec fournisseurs LLM multiples (options futures en légende)
* **adr :** ADR-0002 frontière 2 — stratégie multifournisseur de LLM (interface ③)
* **adr :** ADR-0002 frontière 2 — réversibilité du choix technologique + points d'interface
* **adr :** ADR-0002 §3 — sécabilité érigée en principe d'architecture opposable
* **llm :** guide opérateur du proxy LLM — rôle des clés, opérations courantes, dépannage

### Tests

* **flags :** matrice E2E Docker réelle — 8 combinaisons + contract test plugin, 23/23 PASS
* **llm :** fenêtre de quota large dans le test 429 — évite le flake à la frontière des 60s
* **config :** couvre les 3 nouveaux credentials + one-shot bootstrap_env_overrides
* **ci :** rend test_queue_load_smoke robuste au runner CI
* **ci :** skip propre de test_admin_playwright si playwright absent
* **ci :** rend l'étape Pytest verte (collecte + isolation + integration)

### Maintenance

* **local :** exemple — KEYCLOAK_ISSUER_URL + SSO admin (admin-dm-ui) renseignés
* **local :** harnais dev local persistant (DM + Tempo/Grafana + relay-assistant)
* **lint :** Bandit devient la source unique de SAST (retire S de Ruff)

### Style

* **lint :** CI verte — tri imports (I001), déquote annotation (UP037), E402 bootstrap pré-import, nosec B311 jitter
* **lint :** chaînage d'exceptions + découpe d'instructions → Ruff vert
* **lint :** applique les corrections Ruff sûres (imports, datetime.UTC, f-strings)

### Autres

* **bandit :** durcit ou justifie les findings → Bandit exit 0
* Merge pull request #17 from IA-Generative/sec/admin-observability-and-suggest-hardening
