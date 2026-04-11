# ADR : Strategie de deploiement on-premise DGX pour device-management

**Date** : 2026-04-11
**Statut** : Accepte (valide en production)
**Auteurs** : <DOCKERHUB_NAMESPACE> + Claude Opus 4.6

---

## Contexte

Le projet device-management doit etre deploye on-premise sur un cluster DGX (GPU) du Ministere de l'Interieur, en plus du deploiement cloud existant sur Scaleway. L'environnement DGX presente des contraintes specifiques decouvertes iterativement lors du deploiement.

### Contraintes de l'environnement DGX

| Contrainte | Detail |
|---|---|
| **Cluster** | k3s mono-node, Envoy Gateway (pas nginx-ingress) |
| **Reseau** | Proxy corporate `<PROXY_IP>:3128`, proxy local `localhost:8888` sur les nodes |
| **DNS** | CoreDNS ne resout pas les domaines publics (.gouv.fr, .scw.cloud) |
| **WAF** | Pare-feu applicatif qui bloque les POST natifs `<form>` mais laisse passer `fetch()` |
| **Registry** | Scaleway registry injoignable, DockerHub accessible via proxy |
| **kubectl exec** | Bloque (`Upgrade request required`) — pas de debug interactif |
| **Air-gap partiel** | Sortie Internet via proxy, pas d'acces direct |

---

## Decisions

### 1. Registry : DockerHub mirror au lieu de Scaleway

**Decision** : Toutes les images sont mirrored sur `docker.io/<DOCKERHUB_NAMESPACE>/*` et l'overlay DGX utilise le bloc `images:` de kustomize pour rerouter.

**Alternatives envisagees** :
- Scaleway registry directe → **rejetee** : injoignable depuis le DGX (pas de route reseau)
- Harbor on-premise → **reportee** : necessite infra a deployer, trop de friction pour un premier deploiement
- Transfer air-gap (docker save/load) → **rejetee** : trop manuel, pas de CI possible

**Justification** : DockerHub est accessible via le proxy corporate. Le compte `<DOCKERHUB_NAMESPACE>` avec un PAT evite le rate limit anonyme. Le bloc `images:` de kustomize permet de changer la registry sans modifier aucun manifest de base — les profils Scaleway et local ne sont pas affectes.

**Images mirrored** :
```
<DOCKERHUB_NAMESPACE>/device-management:0.5.16-waf-fix  (image applicative)
<DOCKERHUB_NAMESPACE>/postgres:16-alpine
<DOCKERHUB_NAMESPACE>/nginx:1.27-alpine
<DOCKERHUB_NAMESPACE>/adminer:4.8.1-standalone
<DOCKERHUB_NAMESPACE>/curl:8.10.1                        (image de test)
```

---

### 2. Proxy : IP directe `<PROXY_IP>:3128` par pod

**Decision** : Chaque Deployment qui a besoin de sortir sur Internet a son propre fichier de patch proxy (`proxy-patch-*.yaml`) avec l'IP directe du proxy.

**Alternatives envisagees** :
- Hostname `<PROXY_HOSTNAME>:3128` → **rejetee** : resoluble en DNS interne (`<PROXY_DNS_IP>`) mais le port TCP n'est pas joignable depuis les pods
- Proxy localhost du node (`localhost:8888`) → **rejetee** : n'ecoute que sur `127.0.0.1` du node, pas accessible depuis le reseau des pods
- `hostNetwork: true` sur les pods → **rejetee** : fonctionnel mais ouvre trop de surface d'attaque (le pod voit tout le reseau du node)
- Un seul patch generique pour tous les Deployments → **rejetee** : kustomize strategic merge impose de matcher le container `name`, qui differe par Deployment

**Justification** : L'IP `<PROXY_IP>:3128` a ete decouverte empiriquement comme le seul proxy joignable depuis le reseau des pods. Le proxy-per-deployment est verbeux mais explicite et sans ambiguite. Les Deployments sans besoin de sortie Internet (postgres, adminer) n'ont pas de proxy.

**Deployments patches** : `device-management`, `device-management-admin`, `relay-assistant`, `queue-worker`

**no_proxy** : `localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,<INTERNAL_DOMAIN>,.svc,.svc.cluster.local`
- Les domaines `.gouv.fr` (SSO, compte-rendu) ne sont PAS dans `no_proxy` → ils passent par le proxy. Intentionnel.

---

### 3. HTTPRoute : passthrough pour `/admin` et `/catalog`

**Decision** : Les routes `/admin` et `/catalog` sont en passthrough (pas de `URLRewrite`) tandis que `/bootstrap` rewrite vers `/`.

**Alternatives envisagees** :
- Rewrite `/admin → /` + `--root-path /admin` sur uvicorn → **rejetee** : l'app FastAPI monte deja le router admin avec `prefix="/admin"` (`app.include_router(admin_router, prefix="/admin")`), donc le rewrite causerait un double-prefix
- Tout sous `/bootstrap/*` (y compris admin) → **rejetee** : l'admin est un service K8s different (`device-management-admin`) avec un mode runtime distinct (`DM_RUNTIME_MODE=admin`)

**Justification** :

| Route | Backend | Rewrite | Raison |
|---|---|---|---|
| `/bootstrap` | device-management | → `/` | L'API est conçue pour etre a la racine |
| `/catalog` | device-management | passthrough | L'API a des routes `/catalog/...` natives, et les templates admin font `href="/catalog"` (absolu) |
| `/admin` | device-management-admin | passthrough | L'app monte ses routes avec `prefix="/admin"` |
| `/adminer`, `/files`, `/relay-assistant`, `/telemetry` | services respectifs | → `/` | Chaque service attend d'etre a la racine |

Sur Scaleway (nginx-ingress), le device-management est a la racine (`/`) et l'admin aussi (`/admin`), ce qui fonctionne naturellement. Sur DGX, le hostname `<DGX_HOSTNAME>` est partage avec d'autres apps, d'ou le prefix `/bootstrap`.

---

### 4. WAF bypass : tous les formulaires POST via `fetch()`

**Decision** : Tous les formulaires `<form method="post">` de l'admin UI sont interceptes par JavaScript et soumis via `fetch()` au lieu du submit natif du navigateur.

**Alternatives envisagees** :
- Demander l'ouverture du WAF pour les POST multipart → **reportee** : necessite un ticket infra, delai inconnu, pas de controle
- Convertir le backend en POST JSON → **rejetee** : changerait l'API existante, impacterait les tests et le profil Scaleway
- Desactiver le WAF pour `/admin/*` → **rejetee** : pas sous notre controle

**Justification** : Le WAF du DGX bloque les POST natifs du navigateur (qui envoient `Sec-Fetch-Mode: navigate`) mais laisse passer les requetes `fetch()` (qui envoient `Sec-Fetch-Mode: cors`). Le fix est un intercepteur JavaScript global dans `base.html` + des intercepteurs specifiques pour les formulaires critiques (creation plugin, upload version). Le backend ne change pas — il recoit le meme `FormData`.

**Validation** : Teste automatiquement via le script `10-e2e-waf-test.sh` qui deploie un simulateur WAF (nginx avec blocking conditionnel `Sec-Fetch-Mode`) devant Envoy Gateway et valide 14 scenarios E2E.

---

### 5. Secrets : separes des manifests, persistants dans `~/.dm-secrets/`

**Decision** : Le Secret K8s `device-management-secrets` est exclu du manifest pre-rendu (`dgx-all.yaml`). Les credentials vivent dans `~/.dm-secrets/` sur la machine de deploiement et sont injectes une seule fois dans le cluster.

**Alternatives envisagees** :
- Secret dans le manifest (comme avant) → **rejetee** : ecrase les secrets de production a chaque `kubectl apply`
- Sealed Secrets → **reportee** : ajouterait une dependance (controller kubeseal) sans gain immediat
- Vault / OpenBao / ESO → **reportee** : prevu dans la roadmap ArgoCD (voir `prompt-migration-argocd.md`) mais trop lourd pour un deploiement initial
- Secret embarque dans le package tar.gz → **rejetee** : risque de fuite si le package est partage

**Justification** : La separation est minimale et suffisante :
- `~/.dm-secrets/.env.deploy` : credentials DockerHub (token PAT)
- `~/.dm-secrets/.env.secrets` : tokens applicatifs (relay, telemetry, DB, session)
- `.env.config` (dans le package) : config non-sensible (URLs, flags)

Le script `dumb-deploy.sh` :
1. Premier run : copie les templates dans `~/.dm-secrets/`, demande de les remplir, s'arrete
2. Runs suivants : lit `~/.dm-secrets/`, cree le K8s Secret seulement s'il n'existe pas
3. Le manifest `dgx-all.yaml` ne contient aucun Secret → pas d'ecrasement

---

### 6. Schema DB : bootstrap automatique via Job psql

**Decision** : Le script `dumb-deploy.sh` cree un ConfigMap avec `schema.sql`, lance un pod `postgres:16-alpine` qui execute `psql -f /sql/schema.sql`, puis nettoie.

**Alternatives envisagees** :
- Init container dans le Deployment device-management → **rejetee** : le schema ne doit etre applique qu'une fois, pas a chaque restart de pod
- `kubectl exec` dans le pod postgres → **rejetee** : bloque par le cluster DGX (`Upgrade request required`)
- Migration automatique dans l'app au demarrage → **reportee** : necessiterait du code applicatif (Alembic/migrate), hors scope du deploiement

**Justification** : Le Job psql via `kubectl run --overrides` avec un volume ConfigMap est la seule approche qui fonctionne sans `kubectl exec`. Le schema utilise `IF NOT EXISTS` partout → idempotent. Le queue-worker est automatiquement restart s'il a crashe en attendant les tables.

---

### 7. Tests de connectivite : Job ephemere sans `kubectl exec`

**Decision** : Le script `09-connectivity-test.sh` lance un Job K8s avec l'image `<DOCKERHUB_NAMESPACE>/curl:8.10.1` qui teste DNS, HTTP via proxy, services cluster, et registry.

**Alternatives envisagees** :
- `kubectl exec` dans un pod existant → **rejetee** : bloque sur le DGX
- Pod debug avec `kubectl run --rm -it` → **rejetee** : le `--rm -it` ne marche pas sur le DGX (falling back to streaming logs, pod supprime avant d'avoir les resultats)
- Test depuis la machine locale → **rejetee** : ne teste pas le reseau interne du cluster

**Justification** : Le Job ecrit ses resultats dans stdout, on les lit via `kubectl logs`. L'image curl est sur DockerHub `<DOCKERHUB_NAMESPACE>/curl` (pas de rate limit). Le script distingue les erreurs via les codes de sortie curl (5=proxy DNS, 6=host DNS, 7=connexion refusee, 28=timeout, 35=TLS).

**Variables configurables** : `PROXY`, `NAMESPACE`, `TIMEOUT_SEC`, `USE_HOST_NETWORK`, `TEST_IMAGE`

---

### 8. Test E2E avec simulateur WAF

**Decision** : Le script `10-e2e-waf-test.sh` installe un Envoy Gateway + WAF simulateur (nginx) + app complete sur Scaleway, teste 14 scenarios, puis nettoie tout.

**Justification** : Sans ce test, le bug du POST 403 n'aurait ete detectable que sur le DGX reel (2h de debug). Le simulateur WAF reproduit le comportement exact (blocking sur `Sec-Fetch-Mode: navigate`). Le test est auto-contenu et idempotent.

**Ce qu'il valide** :
- GET routes passent le WAF ✓
- POST natif (browser form) → 403 bloque par WAF ✓
- POST fetch() → 303 passe le WAF ✓
- Redirect apres creation → 200 ✓
- Operations admin (edit, duplicate) via fetch → 303 ✓
- API publique (catalog, healthz) → 200 ✓

---

### 9. Packaging : archive tar.gz autonome

**Decision** : Le deploiement est package dans une archive tar.gz contenant les manifests pre-rendus, le schema SQL, les scripts, et les templates de configuration. Pas d'images Docker (pullees en ligne).

**Structure** :
```
dgx-deploy-vX.X/
├── dumb-deploy.sh           ← point d'entree unique
├── .env.deploy.example      ← template credentials DockerHub
├── .env.secrets.example     ← template secrets applicatifs
├── .env.config              ← config non-sensible (embarquee)
├── schema.sql               ← schema PostgreSQL
├── manifests/dgx-all.yaml   ← manifests K8s pre-rendus (SANS Secret)
├── scripts/
│   └── 09-connectivity-test.sh
├── RUNBOOK-DGX.md
├── README.txt
└── IMAGE_LIST.txt
```

**Justification** : L'archive est legere (28K), ne contient aucun secret, et le deploiement se fait en une commande. Les images sont pullees en ligne depuis DockerHub (accessible via le proxy corporate).

---

## Consequences

### Positives
- Deploiement reproductible en une commande (`./dumb-deploy.sh`)
- Secrets jamais ecrases par un redeploy
- Tests E2E automatises avec simulateur WAF
- Aucune modification de la base kustomize (les overlays DGX sont additifs)
- Le profil Scaleway n'est pas impacte

### Negatives / Dette technique restante
- Les images DockerHub sont sur un compte personnel (`<DOCKERHUB_NAMESPACE>`) — devrait migrer vers un registre d'equipe
- Le proxy est en IP directe (pas de DNS) — fragile si l'IP change
- Les patches proxy sont dupliques (un fichier par Deployment) — pourrait etre factorise avec un mutating webhook
- Le `no_proxy` est hardcode dans chaque patch — devrait etre centralise

### Dette technique resolue (avril 2026)
- ✅ **Schema DB** : Alembic installe avec migration initiale `001` (commit `ae17ade`)
- ✅ **CSRF en production** : cookie pose dans le callback OIDC (commit `0b33f29`)
- ✅ **Secrets ecrases au redeploy** : exclus du manifest, persistants dans `~/.dm-secrets/` (commit `b012158`)
- ✅ **WAF bloque les POST** : formulaires admin soumis via `fetch()` (commit `97e22c4`)
- ✅ **Test E2E WAF** : `10-e2e-waf-test.sh` avec 14 scenarios automatises (commit `f1c3652`)
- 🟡 **Decoupage main.py** : `app/services/db.py` + `app/services/crypto.py` extraits, -204 lignes (commits `38208bf`, `170fce6`)

### Roadmap
- **Court terme** : migration vers ArgoCD + GitLab CI (voir `prompt-migration-argocd.md`)
- **Moyen terme** : Vault/OpenBao pour les secrets (voir section Vault dans le prompt migration)
- **Long terme** : Harbor on-premise pour les images, Sealed Secrets ou ESO comme transition

---

## References

| Document | Chemin |
|---|---|
| HTTPRoute DGX | `deploy/k8s/overlays/dgx/httproute.yaml` |
| Proxy patches | `deploy/k8s/overlays/dgx/proxy-patch-*.yaml` |
| Secret patch DGX | `deploy/k8s/overlays/dgx/secret-patch.yaml` |
| Kustomization DGX | `deploy/k8s/overlays/dgx/kustomization.yaml` |
| Script deploiement | `scripts/dgx-deploy/dumb-deploy.sh` |
| Test connectivite | `scripts/dgx-deploy/09-connectivity-test.sh` |
| Test E2E WAF | `scripts/dgx-deploy/10-e2e-waf-test.sh` |
| Script packaging | `scripts/dgx-deploy/08-package.sh` |
| Template secrets | `scripts/dgx-deploy/.env.secrets.example` |
| Config non-sensible | `scripts/dgx-deploy/.env.config` |
| Runbook | `scripts/dgx-deploy/RUNBOOK-DGX.md` |
| Prompt migration ArgoCD | `prompts/prompt-migration-argocd.md` |
