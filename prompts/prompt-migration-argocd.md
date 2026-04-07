# Migration du deploiement DGX vers ArgoCD + GitLab CI + Vault

## Situation actuelle

Le deploiement est fait manuellement via `kubectl apply -k` avec kustomize.

```
deploy/k8s/
├── base/
│   ├── kustomization.yaml          ← liste 27 manifests
│   ├── manifests/                  ← Deployments, Services, PVCs, HPA, Ingress, ConfigMaps
│   └── secrets/all-secrets.yaml    ← ~50 clefs en clair dans le repo
└── overlays/
    ├── local/                      ← dev (http://bootstrap.home)
    ├── scaleway/                   ← prod cloud (https://<SCALEWAY_HOSTNAME>)
    └── dgx/                        ← on-premise (https://<DGX_HOSTNAME>/bootstrap)
```

Le build est fait a la main avec `scripts/build-k8s.sh` qui pousse vers
la registry Scaleway (`rg.fr-par.scw.cloud`). Le tag est passe en argument.
Le tag est hardcode dans les manifests (pas de `kustomize images`).

Les secrets sont un fichier YAML en clair dans le repo (`all-secrets.yaml`)
avec des overrides par overlay (`secret-patch.yaml`). Pas de rotation
automatique, pas de chiffrement, pas d'audit.

---

## Architecture cible

```
                  ┌──────────────┐
                  │   GitLab     │
                  │   (repo)     │
                  └──────┬───────┘
                         │ push / merge
                         ▼
                  ┌──────────────┐
                  │ GitLab Runner│ ← build image, push registry, update tag
                  │ (CI/CD)      │
                  └──────┬───────┘
                         │ git commit (tag update)
                         ▼
                  ┌──────────────┐
                  │   ArgoCD     │ ← detecte le changement, sync
                  │              │
                  └──────┬───────┘
                         │ kubectl apply
                         ▼
                  ┌──────────────┐
                  │  Cluster DGX │
                  │  (namespace  │
                  │   bootstrap) │
                  └──────────────┘
                         ▲
                         │ inject secrets
                  ┌──────────────┐
                  │ Vault/OpenBao│ (optionnel)
                  └──────────────┘
```

---

## Etape 1 — Preparer le repo pour ArgoCD

ArgoCD sait deployer directement du kustomize. Pas besoin de migrer
vers Helm si on ne veut pas. La structure actuelle est deja compatible.

### 1.1 Supprimer les secrets du repo

**C'est le prerequis bloquant.** ArgoCD sync un repo git → les secrets
ne doivent plus etre en clair dans le repo.

Le fichier `deploy/k8s/base/secrets/all-secrets.yaml` contient ~50 clefs
dont des tokens, passwords, et clefs de chiffrement.

**Action :**

1. Retirer `secrets/all-secrets.yaml` de la kustomization base
2. Retirer les `secret-patch.yaml` des overlays
3. Choisir une strategie de secrets (voir Etape 5)
4. Ajouter `deploy/k8s/base/secrets/` dans `.gitignore`

### 1.2 Externaliser le tag image

Actuellement le tag `0.5.15` est hardcode dans 4 manifests. Kustomize
sait overrider les images sans toucher aux manifests :

Ajouter dans chaque `kustomization.yaml` d'overlay :

```yaml
images:
  - name: <SCALEWAY_REGISTRY>/device-management
    newName: registry.gitlab.example.com/mirai/device-management
    newTag: "0.6.0"
```

Le CI mettra a jour `newTag` a chaque build via :

```bash
cd deploy/k8s/overlays/dgx
kustomize edit set image \
  "registry.gitlab.example.com/mirai/device-management=registry.gitlab.example.com/mirai/device-management:${CI_COMMIT_SHORT_SHA}"
```

Les 4 manifests qui utilisent l'image sont :
- `20-device-management-deployment.yaml` (main API)
- `22-admin-deployment.yaml` (admin UI)
- `23-telemetry-relay-deployment.yaml`
- `28-queue-worker-deployment.yaml` (init container uses postgres image, main uses DM image)

### 1.3 Nettoyer imagePullPolicy

Passer de `imagePullPolicy: Always` a `IfNotPresent` une fois que les
tags sont immutables (SHA ou semver). `Always` force un pull a chaque
restart, ce qui est lent en air-gap.

---

## Etape 2 — GitLab CI Pipeline

Creer `.gitlab-ci.yml` a la racine du repo.

### 2.1 Structure du pipeline

```yaml
stages:
  - test
  - build
  - update-manifests

variables:
  # Registry GitLab (chaque projet a une registry integree)
  IMAGE_NAME: ${CI_REGISTRY_IMAGE}/device-management
  # OU registry on-premise :
  # IMAGE_NAME: <DGX_REGISTRY>/mirai/device-management

test:
  stage: test
  image: python:3.12-slim
  script:
    - pip install -r requirements.txt
    - python -m pytest tests/ -v
  rules:
    - if: $CI_MERGE_REQUEST_ID
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

build:
  stage: build
  image: docker:27
  services:
    - docker:27-dind
  variables:
    DOCKER_TLS_CERTDIR: "/certs"
  before_script:
    - docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY
  script:
    - docker buildx create --use
    - docker buildx build
        --platform linux/amd64
        -t ${IMAGE_NAME}:${CI_COMMIT_SHORT_SHA}
        -t ${IMAGE_NAME}:latest
        -f deploy/docker/Dockerfile
        --push .
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

update-manifests:
  stage: update-manifests
  image: bitnami/kubectl:latest
  script:
    - cd deploy/k8s/overlays/dgx
    - |
      # Mettre a jour le tag image dans kustomization.yaml
      sed -i "s|newTag:.*|newTag: \"${CI_COMMIT_SHORT_SHA}\"|" kustomization.yaml
    - cd $CI_PROJECT_DIR
    - git config user.name "GitLab CI"
    - git config user.email "ci@example.com"
    - git add deploy/k8s/overlays/dgx/kustomization.yaml
    - git commit -m "ci: update DGX image tag to ${CI_COMMIT_SHORT_SHA}" || true
    - git push https://gitlab-ci-token:${MANIFEST_PUSH_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git HEAD:${CI_COMMIT_BRANCH}
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
```

### 2.2 Si on quitte Scaleway Registry

Trois options pour la registry d'images :

| Option | Avantage | Inconvenient |
|--------|----------|--------------|
| GitLab Container Registry | Integree, gratuite, auth CI native | Necessite que le DGX puisse pull depuis GitLab |
| Registry on-premise (Harbor) | Air-gap natif, scan de vulnerabilites | Infrastructure a maintenir |
| Transfer air-gap (docker save) | Fonctionne sans reseau | Manuel, pas de CI automatise |

**Si GitLab Registry :** le runner build et pousse dans `$CI_REGISTRY`.
Le DGX pull via le proxy corporate.

**Si Harbor on-premise :** le runner pousse dans Harbor. Le DGX pull en
local (pas de proxy). Ajouter le provider `harbor` dans
`create-registry-secret.sh`.

**Si air-gap strict :** le CI produit un tarball comme `08-package.sh`.
ArgoCD n'est pas pertinent dans ce cas (pas de sync git auto).

### 2.3 Adapter create-registry-secret.sh

Ajouter un provider `gitlab` :

```bash
gitlab)
  SERVER="${CI_REGISTRY:-registry.gitlab.example.com}"
  USERNAME="${REGISTRY_USERNAME:-gitlab-ci-token}"
  PASSWORD="${REGISTRY_PASSWORD:-${CI_REGISTRY_PASSWORD:-}}"
  ;;
```

### 2.4 Proxy pour le Runner

Si le GitLab Runner est dans le DGX, il aura besoin du proxy pour
acceder a GitLab et a la registry :

```toml
# /etc/gitlab-runner/config.toml
[[runners]]
  environment = [
    "https_proxy=http://<PROXY_HOSTNAME>:3128",
    "http_proxy=http://<PROXY_HOSTNAME>:3128",
    "no_proxy=localhost,127.0.0.1,.minint.fr,.svc,.svc.cluster.local"
  ]
```

---

## Etape 3 — Installer ArgoCD sur le DGX

### 3.1 Installation

```bash
kubectl create namespace argocd

# Install ArgoCD (version stable)
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

En air-gap, telecharger le manifest et les images, les transferer,
puis `ctr -n k8s.io images import` + `kubectl apply -f`.

### 3.2 Proxy pour ArgoCD

ArgoCD a besoin de pull le repo git. Dans le DGX, il faut le proxy :

```bash
kubectl -n argocd set env deploy/argocd-repo-server \
  HTTPS_PROXY=http://<PROXY_HOSTNAME>:3128 \
  HTTP_PROXY=http://<PROXY_HOSTNAME>:3128 \
  NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,.minint.fr,.svc,.svc.cluster.local
```

### 3.3 Configurer le repo Git

```bash
argocd repo add https://gitlab.example.com/mirai/device-management.git \
  --username gitlab-ci-token \
  --password <DEPLOY_TOKEN> \
  --proxy http://<PROXY_HOSTNAME>:3128
```

### 3.4 Creer l'Application ArgoCD

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: device-management
  namespace: argocd
spec:
  project: default

  source:
    repoURL: https://gitlab.example.com/mirai/device-management.git
    targetRevision: main
    path: deploy/k8s/overlays/dgx

  destination:
    server: https://kubernetes.default.svc
    namespace: bootstrap

  syncPolicy:
    automated:
      prune: true        # supprime les ressources qui ne sont plus dans git
      selfHeal: true      # corrige les drifts manuels
    syncOptions:
      - CreateNamespace=true
      - ApplyOutOfSyncOnly=true
```

### 3.5 Sync manuel vs automatique

| Mode | Quand l'utiliser |
|------|-----------------|
| `automated` (ci-dessus) | Environnement stable, confiance dans le CI |
| Manuel (`argocd app sync device-management`) | Premier deploiement, debug, production critique |

Pour le premier deploiement, commencer en sync manuel puis passer
en automatique une fois valide.

---

## Etape 4 — Structure kustomize adaptee a ArgoCD

La structure actuelle est compatible. Quelques ajustements :

### 4.1 Ajouter les labels ArgoCD

ArgoCD ajoute automatiquement ses labels. Rien a faire.

### 4.2 Ajouter le bloc images dans kustomization.yaml DGX

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base
  - httproute.yaml

images:
  - name: <SCALEWAY_REGISTRY>/device-management
    newName: registry.gitlab.example.com/mirai/device-management
    newTag: "abc1234"

patches:
  - path: secret-patch.yaml
  - path: proxy-patch-device-management.yaml
    target:
      kind: Deployment
      name: device-management
  - path: proxy-patch-relay-assistant.yaml
    target:
      kind: Deployment
      name: relay-assistant
  - path: proxy-patch-queue-worker.yaml
    target:
      kind: Deployment
      name: queue-worker
```

### 4.3 Helm chart (alternative optionnelle)

Si l'equipe veut migrer vers Helm a terme, voici la structure :

```
deploy/helm/device-management/
├── Chart.yaml
├── values.yaml                    ← valeurs par defaut
├── values-dgx.yaml               ← override DGX
├── templates/
│   ├── _helpers.tpl
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── deployment-dm.yaml
│   ├── deployment-admin.yaml
│   ├── deployment-relay.yaml
│   ├── deployment-worker.yaml
│   ├── deployment-postgres.yaml
│   ├── service-dm.yaml
│   ├── service-relay.yaml
│   ├── service-postgres.yaml
│   ├── hpa.yaml
│   ├── httproute.yaml             ← conditionnel selon .Values.gateway.enabled
│   ├── ingress.yaml               ← conditionnel selon .Values.ingress.enabled
│   └── secret.yaml                ← conditionnel, voir Vault
└── crds/                          ← vide pour l'instant
```

Avantages Helm vs Kustomize :
- Templating natif (boucles, conditions)
- `helm diff` pour preview
- Rollback natif (`helm rollback`)
- Gestion des CRDs
- ArgoCD supporte les deux

Inconvenients :
- Plus complexe, courbe d'apprentissage
- Les manifests deviennent des templates (moins lisibles)
- Necessite de maintenir values.yaml

**Recommandation :** rester en kustomize pour l'instant. ArgoCD le
supporte nativement. Migrer vers Helm quand il y aura plus de 3
environnements ou des besoins de templating complexes.

---

## Etape 5 — Gestion des secrets avec Vault / OpenBao

### 5.1 Deux approches possibles

| Approche | Composant | Comment ca marche |
|----------|-----------|-------------------|
| **A. External Secrets Operator (ESO)** | ESO + Vault/OpenBao | ESO lit les secrets dans Vault et cree les Secrets K8s |
| **B. Vault Agent Injector** | Vault sidecar | Un sidecar injecte les secrets dans les pods via annotations |

**Recommandation : approche A (ESO)** — moins invasive, pas de sidecar,
compatible avec le fonctionnement actuel (les pods lisent des env vars
depuis un Secret K8s).

### 5.2 Installer External Secrets Operator

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace
```

### 5.3 Configurer la connexion a Vault / OpenBao

```yaml
# deploy/k8s/base/manifests/05-secret-store.yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: vault-backend
  namespace: bootstrap
spec:
  provider:
    vault:
      server: "https://vault.minint.fr"       # ou http://openbao.svc:8200
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "device-management"
          # OU token auth :
          # tokenSecretRef:
          #   name: vault-token
          #   key: token
```

### 5.4 Creer l'ExternalSecret

Remplace `all-secrets.yaml` par un ExternalSecret qui dit a ESO
quoi aller chercher dans Vault :

```yaml
# deploy/k8s/base/manifests/06-external-secret.yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: device-management-secrets
  namespace: bootstrap
spec:
  refreshInterval: 5m                    # re-sync depuis Vault toutes les 5 min
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: device-management-secrets      # le Secret K8s cree par ESO
    creationPolicy: Owner
  data:
    # --- Database ---
    - secretKey: DATABASE_URL
      remoteRef:
        key: device-management/database
        property: url

    - secretKey: DATABASE_ADMIN_URL
      remoteRef:
        key: device-management/database
        property: admin_url

    - secretKey: POSTGRES_PASSWORD
      remoteRef:
        key: device-management/database
        property: password

    # --- Keycloak ---
    - secretKey: KEYCLOAK_ISSUER_URL
      remoteRef:
        key: device-management/keycloak
        property: issuer_url

    - secretKey: KEYCLOAK_REALM
      remoteRef:
        key: device-management/keycloak
        property: realm

    - secretKey: KEYCLOAK_CLIENT_ID
      remoteRef:
        key: device-management/keycloak
        property: client_id

    # --- Relay ---
    - secretKey: DM_RELAY_PROXY_SHARED_TOKEN
      remoteRef:
        key: device-management/relay
        property: proxy_shared_token

    - secretKey: DM_RELAY_SECRET_PEPPER
      remoteRef:
        key: device-management/relay
        property: secret_pepper

    - secretKey: RELAY_KEYCLOAK_UPSTREAM
      remoteRef:
        key: device-management/relay
        property: keycloak_upstream

    - secretKey: RELAY_LLM_UPSTREAM
      remoteRef:
        key: device-management/relay
        property: llm_upstream

    - secretKey: RELAY_MCR_API_UPSTREAM
      remoteRef:
        key: device-management/relay
        property: mcr_api_upstream

    - secretKey: RELAY_COMPTE_RENDU_UPSTREAM
      remoteRef:
        key: device-management/relay
        property: compte_rendu_upstream

    # --- Telemetry ---
    - secretKey: DM_TELEMETRY_TOKEN_SIGNING_KEY
      remoteRef:
        key: device-management/telemetry
        property: token_signing_key

    - secretKey: DM_TELEMETRY_UPSTREAM_KEY
      remoteRef:
        key: device-management/telemetry
        property: upstream_key

    # --- AWS / S3 ---
    - secretKey: AWS_ACCESS_KEY_ID
      remoteRef:
        key: device-management/s3
        property: access_key_id

    - secretKey: AWS_SECRET_ACCESS_KEY
      remoteRef:
        key: device-management/s3
        property: secret_access_key

    # --- LLM ---
    - secretKey: LLM_API_TOKEN
      remoteRef:
        key: device-management/llm
        property: api_token
```

### 5.5 Alimenter Vault

Structure recommandee dans Vault :

```
secret/device-management/
├── database
│   ├── url              = postgresql://dev:dev@postgres:5432/bootstrap
│   ├── admin_url        = postgresql://postgres:postgres@postgres:5432/postgres
│   └── password         = <mot de passe postgres>
├── keycloak
│   ├── issuer_url       = https://<DGX_HOSTNAME>/relay-assistant/keycloak
│   ├── realm            = mirai
│   └── client_id        = bootstrap-iassistant
├── relay
│   ├── proxy_shared_token    = <token>
│   ├── secret_pepper         = <pepper>
│   ├── keycloak_upstream     = https://<SSO_HOSTNAME>/realms/mirai
│   ├── llm_upstream          = https://<LLM_API_HOSTNAME>/v1
│   ├── mcr_api_upstream      = https://<MCR_PLACEHOLDER>
│   └── compte_rendu_upstream = https://<COMPTERENDU_HOSTNAME>
├── telemetry
│   ├── token_signing_key = <key>
│   └── upstream_key      = <key>
├── s3
│   ├── access_key_id     = <key>
│   └── secret_access_key = <secret>
└── llm
    └── api_token         = <token>
```

Commandes pour injecter (Vault CLI) :

```bash
vault kv put secret/device-management/database \
  url="postgresql://dev:dev@postgres:5432/bootstrap" \
  admin_url="postgresql://postgres:postgres@postgres:5432/postgres" \
  password="<MOT_DE_PASSE>"

vault kv put secret/device-management/keycloak \
  issuer_url="https://<DGX_HOSTNAME>/relay-assistant/keycloak" \
  realm="mirai" \
  client_id="bootstrap-iassistant"

vault kv put secret/device-management/relay \
  proxy_shared_token="<TOKEN>" \
  secret_pepper="<PEPPER>" \
  keycloak_upstream="https://<SSO_HOSTNAME>/realms/mirai" \
  llm_upstream="https://<LLM_API_HOSTNAME>/v1" \
  mcr_api_upstream="https://<MCR_PLACEHOLDER>" \
  compte_rendu_upstream="https://<COMPTERENDU_HOSTNAME>"

# etc.
```

### 5.6 Separer secrets et config

Le `all-secrets.yaml` actuel melange des vraies secrets (tokens,
passwords) et de la config non-sensible (ports, flags, URLs publiques).

**Action :** Deplacer la config non-sensible dans un ConfigMap :

```yaml
# deploy/k8s/base/manifests/11-configmap-app-settings.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: device-management-config
  namespace: bootstrap
data:
  DM_APP_ENV: "prod"
  DM_CONFIG_ENABLED: "true"
  DM_CONFIG_PROFILE: "prod"
  DM_ALLOW_ORIGINS: "*"
  DM_MAX_BODY_SIZE_MB: "10"
  DM_PORT: "3001"
  DM_STORE_ENROLL_LOCALLY: "true"
  DM_ENROLL_DIR: "/data/enroll"
  DM_STORE_ENROLL_S3: "false"
  DM_AUTH_VERIFY_ACCESS_TOKEN: "true"
  DM_AUTH_ALLOWED_ALGORITHMS_CSV: "RS256"
  DM_AUTH_LEEWAY_SECONDS: "30"
  DM_AUTH_JWKS_CACHE_TTL_SECONDS: "600"
  DM_RELAY_ENABLED: "true"
  DM_RELAY_KEY_TTL_SECONDS: "2592000"
  DM_RELAY_REQUIRE_KEY_FOR_SECRETS: "true"
  DM_TELEMETRY_ENABLED: "true"
  DM_TELEMETRY_PUBLIC_ENDPOINT: "/telemetry/v1/traces"
  DM_TELEMETRY_AUTHORIZATION_TYPE: "Bearer"
  DM_TELEMETRY_REQUIRE_TOKEN: "true"
  DM_TELEMETRY_MAX_BODY_SIZE_MB: "2"
  DM_BINARIES_MODE: "local"
  DM_PRESIGN_TTL_SECONDS: "300"
```

Puis dans les Deployments, utiliser `envFrom` avec les deux sources :

```yaml
envFrom:
  - configMapRef:
      name: device-management-config
  - secretRef:
      name: device-management-secrets   # cree par ESO depuis Vault
```

Cela simplifie les Deployments (plus de 30 blocs `env:` individuels)
et separe clairement ce qui est versionne dans git (ConfigMap) de ce
qui est dans Vault (Secret).

### 5.7 Overlay DGX : override config via patch

Les valeurs specifiques DGX (URLs, targets) deviennent des patches
kustomize sur le ConfigMap :

```yaml
# deploy/k8s/overlays/dgx/config-patch.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: device-management-config
  namespace: bootstrap
data:
  DM_RELAY_ALLOWED_TARGETS_CSV: "keycloak,config,llm,mcr-api,telemetry,compte-rendu"
  DM_ENROLL_URL: "/bootstrap/enroll"
  PUBLIC_BASE_URL: "https://<DGX_HOSTNAME>/bootstrap"
```

Les URLs sensibles (upstream keycloak, tokens) restent dans Vault
et sont injectees par ESO.

### 5.8 Si Vault n'est pas disponible : Sealed Secrets

Alternative sans infrastructure Vault :

```bash
# Installer kubeseal
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets -n kube-system

# Chiffrer le secret existant
kubeseal --format yaml < deploy/k8s/base/secrets/all-secrets.yaml \
  > deploy/k8s/base/secrets/sealed-secrets.yaml
```

Le fichier chiffre peut rester dans git. Seul le cluster peut le
dechiffrer. Moins puissant que Vault (pas de rotation, pas d'audit)
mais zero infrastructure supplementaire.

---

## Etape 6 — Plan de migration progressif

Ne pas tout faire d'un coup. Migrer par palier.

### Palier 1 : GitLab CI pour le build (1 jour)

- [ ] Creer `.gitlab-ci.yml` avec les stages test + build
- [ ] Configurer la registry (GitLab ou Harbor)
- [ ] Adapter `.env.registry` pour le nouveau provider
- [ ] Tester le build en CI
- [ ] Mettre a jour `create-registry-secret.sh` avec le provider gitlab/harbor

Impact : le build est automatise mais le deploiement reste manuel.

### Palier 2 : Externaliser le tag image (0.5 jour)

- [ ] Ajouter le bloc `images:` dans les kustomization.yaml d'overlays
- [ ] Ajouter le stage `update-manifests` dans le CI
- [ ] Supprimer le tag hardcode des manifests base (mettre un placeholder)
- [ ] Valider avec `kubectl kustomize`

Impact : le CI met a jour le tag automatiquement apres chaque build.

### Palier 3 : Installer ArgoCD (0.5 jour)

- [ ] Installer ArgoCD dans le cluster DGX
- [ ] Configurer le proxy sur argocd-repo-server
- [ ] Ajouter le repo git avec credentials
- [ ] Creer l'Application ArgoCD (sync MANUEL d'abord)
- [ ] Valider un sync manuel complet

Impact : le deploiement se fait depuis ArgoCD (UI ou CLI).

### Palier 4 : Separer secrets et config (1 jour)

- [ ] Creer le ConfigMap pour la config non-sensible
- [ ] Migrer les Deployments vers `envFrom` (configMapRef + secretRef)
- [ ] Tester que tout fonctionne toujours avec les secrets en K8s natif
- [ ] Ne pas encore toucher a Vault

Impact : preparation pour Vault, les secrets sont isoles.

### Palier 5 : Vault / OpenBao + ESO (1-2 jours)

- [ ] Installer Vault/OpenBao (ou se connecter a un existant)
- [ ] Installer External Secrets Operator
- [ ] Creer le SecretStore + ExternalSecret
- [ ] Alimenter Vault avec les clefs
- [ ] Supprimer `all-secrets.yaml` et les `secret-patch.yaml` du repo
- [ ] Ajouter `deploy/k8s/base/secrets/` dans `.gitignore`
- [ ] Passer ArgoCD en sync automatique

Impact : les secrets ne sont plus dans git, rotation possible.

### Palier 6 : Sync automatique ArgoCD (0.5 jour)

- [ ] Passer `syncPolicy.automated` a true
- [ ] Tester le cycle complet : push git → CI build → tag update → ArgoCD sync
- [ ] Configurer les notifications ArgoCD (Slack, email)

---

## Resume des fichiers a creer ou modifier

| Fichier | Action | Palier |
|---------|--------|--------|
| `.gitlab-ci.yml` | Creer | 1 |
| `.env.registry` | Adapter provider | 1 |
| `scripts/k8s/create-registry-secret.sh` | Ajouter provider gitlab | 1 |
| `deploy/k8s/overlays/dgx/kustomization.yaml` | Ajouter bloc `images:` | 2 |
| `deploy/k8s/overlays/scaleway/kustomization.yaml` | Ajouter bloc `images:` | 2 |
| ArgoCD Application YAML | Creer | 3 |
| `deploy/k8s/base/manifests/11-configmap-app-settings.yaml` | Creer | 4 |
| `deploy/k8s/base/manifests/20-device-management-deployment.yaml` | Migrer vers envFrom | 4 |
| `deploy/k8s/base/manifests/28-queue-worker-deployment.yaml` | Migrer vers envFrom | 4 |
| `deploy/k8s/base/manifests/05-secret-store.yaml` | Creer (ESO) | 5 |
| `deploy/k8s/base/manifests/06-external-secret.yaml` | Creer (ESO) | 5 |
| `deploy/k8s/base/secrets/all-secrets.yaml` | Supprimer | 5 |
| `deploy/k8s/overlays/*/secret-patch.yaml` | Supprimer | 5 |
| `.gitignore` | Ajouter secrets/ | 5 |
