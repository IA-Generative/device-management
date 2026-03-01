# deploy-dgx

Deploiement Kubernetes DGX on-prem, pilote depuis une machine de rebond avec `kubectl`.

Ce README est organise en 2 parties:
1. usage quotidien (operations)
2. installation initiale + tests

## 1) Usage quotidien

### 1.1 Verifier le contexte kubectl

Avant toute action:
```bash
./deploy-dgx/scripts/preflight-dgx.sh
```

Les scripts DGX affichent toujours:
- `current-context`
- `cluster`
- `server`
- `default-namespace`

Options utiles:
- `DGX_EXPECTED_CONTEXT=<nom>`: warning si le contexte differe
- `DGX_SKIP_CONTEXT_CONFIRM=1`: mode non interactif

### 1.2 Choisir le mode de stockage/fetch (local, S3 proxy, S3 presign)

Les valeurs se pilotent via `deploy-dgx/settings.yaml` + secret applicatif.

Mode `local` (DGX sans acces S3 direct recommande):
- `dm_store_enroll_s3: "false"`
- `dm_binaries_mode: local`
- les fichiers sont servis depuis le PVC local (`/data/content`)
- route d'administration fichiers: `https://<host>/files`

Mode `proxy` (S3 reachable depuis pod):
- `dm_store_enroll_s3: "true"`
- `dm_binaries_mode: proxy`
- le service telecharge depuis S3 et re-sert via `/bootstrap/binaries/...`

Mode `presign` (clients doivent atteindre S3):
- `dm_store_enroll_s3: "true"`
- `dm_binaries_mode: presign`
- le service renvoie une URL signee S3 (redirect)

Apres modification:
```bash
./deploy-dgx/scripts/render-from-settings.sh
./deploy-dgx/deploy-full-dgx.sh
```

### 1.3 Modifier les fichiers de config au quotidien

Option simple (non technique):
- utiliser Filebrowser via `https://<host>/files`
- modifier les JSON sous `config/`

Option Git/terminal:
- modifier `deploy-dgx/settings.yaml` et/ou `deploy-dgx/secrets/10-device-management-secret.yaml`
- regenirer puis redeployer:
```bash
./deploy-dgx/scripts/render-from-settings.sh
./deploy-dgx/deploy-full-dgx.sh
```

### 1.4 Ajouter/mettre a jour les binaries

En mode `local`:
- deposer les fichiers via Filebrowser dans `binaries/...`
- tests auto attendent aussi `binaries/test/test.json` et `binaries/test/ok.png`

En mode `proxy`/`presign`:
- uploader dans S3 sous le prefixe `DM_S3_PREFIX_BINARIES` (ex: `binaries/...`)
- verifier bucket/prefix dans `10-device-management-secret.yaml`

### 1.5 Mettre a jour l'image de l'application

Workflow interactif:
```bash
./deploy-dgx/update-deployment-dgx.sh
```

Ce script:
- propose changement de tag
- met a jour le manifest si besoin
- force un rollout restart si tag identique

### 1.6 Credentials registry persistants (machine de rebond)

Configuration interactive (DockerHub/Scaleway/custom) + apply secret:
```bash
./deploy-dgx/scripts/configure-registry-dgx.sh --apply
```

Apply seul depuis les variables deja sauvegardees:
```bash
./deploy-dgx/create-registry-secret.sh
```

Le fichier local persistant est:
- `deploy-dgx/.env.registry`

### 1.7 Commandes operationnelles courantes

Deploiement complet (create/update + smoke test auto):
```bash
./deploy-dgx/deploy-full-dgx.sh
```

Deploiement secrets uniquement:
```bash
./deploy-dgx/deploy-secrets-dgx.sh
```

Smoke test manuel:
```bash
./deploy-dgx/scripts/smoke-test-dgx.sh
```

Smoke test externe (routes gateway):
```bash
DGX_TEST_EXTERNAL=1 ./deploy-dgx/scripts/smoke-test-dgx.sh
```

Si certificat externe non valide:
```bash
DGX_TEST_EXTERNAL=1 DGX_TEST_EXTERNAL_INSECURE=1 ./deploy-dgx/scripts/smoke-test-dgx.sh
```

Desactiver le smoke test auto dans `deploy-full`:
```bash
DGX_SKIP_SMOKE_TEST=1 ./deploy-dgx/deploy-full-dgx.sh
```

## 2) Installation initiale et tests

### 2.1 Pre-requis

Depuis la machine de rebond:
- acces API Kubernetes DGX via `kubectl`
- namespace cible (ex: `bootstrap`)
- acces image registry (DockerHub/Scaleway/custom)

### 2.2 Installation initiale recommandee

Commande unique (avec reset namespace):
```bash
./deploy-dgx/install-initial-dgx.sh --reset-namespace
```

Le script enchaine:
1. configuration interactive (`settings` + secrets)
2. preflight
3. reset namespace (optionnel)
4. configuration credentials registry + creation `regcred`
5. deploiement complet

### 2.3 Installation en etapes manuelles

1) Configurer settings/secrets
```bash
./deploy-dgx/scripts/configure-interactive-dgx.sh
```

2) Configurer registry
```bash
./deploy-dgx/scripts/configure-registry-dgx.sh --apply
```

3) Preflight
```bash
./deploy-dgx/scripts/preflight-dgx.sh
```

4) Reset namespace (optionnel)
```bash
./deploy-dgx/reset-namespace-dgx.sh
```

5) Deployer
```bash
./deploy-dgx/deploy-full-dgx.sh
```

### 2.4 Validation de l'installation

`deploy-full-dgx.sh` lance un smoke test complet qui valide:
- rollout: `postgres`, `device-management`, `adminer`, `filebrowser`
- endpoints services
- checks fonctionnels device-management:
  - `/livez`
  - `/config/matisse/config.json`
  - `/config/libreoffice/config.json`
  - acces interne `adminer` et `filebrowser`
  - connectivite `postgres:5432`
- en mode `local`: verification de `/binaries/test/test.json` et `/binaries/test/ok.png`

Implementation smoke test:
- via `Job` Kubernetes (pas `kubectl exec`), compatible clusters qui refusent les upgrades `exec/port-forward`

### 2.5 Initialisation auto du contenu local (mode local)

Dans `deploy-dgx/manifests/20-device-management-deployment.yaml`, l'init container:
- initialise `config/` depuis `/app/config` si vide
- cree `binaries/test/ok.png` + `binaries/test/test.json` si absents

Donc un PVC deja existant reste exploitable, et les assets de smoke test sont recrees si necessaire.

## Annexes

### Structure du dossier

- `manifests/`: 1 fichier par objet K8s
- `secrets/`: secrets individuels + `all-secrets.yaml`
- `settings.yaml`: valeurs de routing/mode
- `.env.registry`: credentials registry persistants (local rebond)
- `.env.registry.example`: exemple de credentials registry
- `scripts/`: configuration, preflight, render, smoke
- `install-initial-dgx.sh`: install guidee
- `update-deployment-dgx.sh`: update image/tag
- `deploy-full-dgx.sh`: apply complet

### Git / push GitHub

Le repo contient des fichiers d'exemple versionnes:
- `deploy-dgx/.env.registry.example`
- `deploy-dgx/secrets/10-device-management-secret-example.yaml`
- `deploy-dgx/secrets/20-registry-secret-example.yaml`
- `deploy-dgx/secrets/30-filebrowser-users-secret-example.yaml`

Les fichiers reels restent ignores (ne partent pas au push):
- `deploy-dgx/.env.registry`
- `deploy-dgx/secrets/10-device-management-secret.yaml`
- `deploy-dgx/secrets/20-registry-secret.yaml`
- `deploy-dgx/secrets/30-filebrowser-users-secret.yaml`
- `deploy-dgx/secrets/all-secrets.yaml`

Initialisation locale des fichiers reels depuis les exemples:
```bash
./deploy-dgx/scripts/init-secrets-from-example.sh
```

Verification rapide:
```bash
git check-ignore -v deploy-dgx/.env.registry deploy-dgx/secrets/10-device-management-secret.yaml deploy-dgx/secrets/all-secrets.yaml
git status --short deploy-dgx
```

### Comptes Filebrowser (10 comptes)

Le secret `filebrowser-users-secrets` contient `editor1` ... `editor10`.

Rotation rapide:
```bash
kubectl -n bootstrap patch secret filebrowser-users-secrets \
  --type merge \
  -p '{"stringData":{
    "EDITOR1_PASSWORD":"<new-pass-1>",
    "EDITOR2_PASSWORD":"<new-pass-2>",
    "EDITOR3_PASSWORD":"<new-pass-3>",
    "EDITOR4_PASSWORD":"<new-pass-4>",
    "EDITOR5_PASSWORD":"<new-pass-5>",
    "EDITOR6_PASSWORD":"<new-pass-6>",
    "EDITOR7_PASSWORD":"<new-pass-7>",
    "EDITOR8_PASSWORD":"<new-pass-8>",
    "EDITOR9_PASSWORD":"<new-pass-9>",
    "EDITOR10_PASSWORD":"<new-pass-10>"
  }}'

kubectl -n bootstrap delete job filebrowser-users-init --ignore-not-found
kubectl apply -f deploy-dgx/manifests/51-filebrowser-users-job.yaml
kubectl -n bootstrap wait --for=condition=complete --timeout=180s job/filebrowser-users-init
```

### Argo CD

`deploy-dgx/kustomization.yaml` pointe vers les manifests separes.

1. Modifier `repoURL`/`targetRevision` dans `deploy-dgx/argocd/application.yaml`
2. Appliquer:
```bash
kubectl apply -f deploy-dgx/argocd/application.yaml
```

Note registry:
- `all-secrets.yaml` n'inclut pas `regcred`
- creer/mettre a jour `regcred` avec `./deploy-dgx/create-registry-secret.sh` avant `deploy-full`
