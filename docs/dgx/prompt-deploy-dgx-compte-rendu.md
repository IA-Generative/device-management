# Deploiement DGX — Proxy pods + relay compte-rendu

## Etat actuel

Les modifications sont **deja appliquees** dans le repo. Ce document
sert de reference pour comprendre ce qui a change et pourquoi.

### Proxy corporate sur tous les pods sortants

Proxy : `https_proxy=http://<PROXY_HOSTNAME>:3128`

Avant : seul `device-management` avait le proxy.
Maintenant : 3 patches separees (strategic merge par container name) :

```
deploy/k8s/overlays/dgx/
├── proxy-patch-device-management.yaml   (container: device-management)
├── proxy-patch-relay-assistant.yaml     (container: relay-assistant)
├── proxy-patch-queue-worker.yaml        (container: queue-worker)
└── kustomization.yaml                   (3 targets proxy)
```

Variables injectees dans chaque pod :
- `https_proxy` / `HTTPS_PROXY`
- `http_proxy` / `HTTP_PROXY`
- `no_proxy` / `NO_PROXY` = `localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,.<INTERNAL_DOMAIN>,.svc,.svc.cluster.local`

Les domaines `.gouv.fr` (sso.mirai, compte-rendu.mirai) ne sont PAS dans
`no_proxy` → ils passent par le proxy. C'est intentionnel.

L'ancien `proxy-patch.yaml` est remplace par les 3 fichiers per-deployment.

### Nouveau relay compte-rendu

Fichiers modifies dans la **base** (impacte tous les profils) :

`deploy/k8s/base/manifests/25-relay-assistant-configmap.yaml` :
```nginx
location ~ ^/compte-rendu/(.+)$ {
  set $relay_target compte-rendu;
  auth_request /__relay_auth;
  proxy_set_header Authorization $http_authorization;
  proxy_set_header Content-Type $http_content_type;
  proxy_set_header User-Agent $http_user_agent;
  proxy_pass ${RELAY_COMPTE_RENDU_UPSTREAM}/$1$is_args$args;
}
```

`deploy/k8s/base/manifests/26-relay-assistant-deployment.yaml` :
```yaml
- name: RELAY_COMPTE_RENDU_UPSTREAM
  valueFrom:
    secretKeyRef:
      name: device-management-secrets
      key: RELAY_COMPTE_RENDU_UPSTREAM
```

`deploy/k8s/base/secrets/all-secrets.yaml` — placeholder ajoute.

### Secrets DGX

`deploy/k8s/overlays/dgx/env-secrets.yaml` :

```yaml
stringData:
  PUBLIC_BASE_URL: "https://<DGX_HOSTNAME>/bootstrap"
  DM_ENROLL_URL: "/bootstrap/enroll"
  KEYCLOAK_ISSUER_URL: "https://<DGX_HOSTNAME>/relay-assistant/keycloak"
  RELAY_KEYCLOAK_UPSTREAM: "https://<SSO_HOSTNAME>/realms/mirai"
  RELAY_COMPTE_RENDU_UPSTREAM: "https://<COMPTERENDU_HOSTNAME>"
  DM_RELAY_ALLOWED_TARGETS_CSV: "keycloak,config,llm,mcr-api,telemetry,compte-rendu"
  DM_AUTH_JWKS_URL: "https://<SSO_HOSTNAME>/realms/mirai/protocol/openid-connect/certs"
```

---

## Scripts de deploiement

```
scripts/dgx-deploy/
├── 09-connectivity-test.sh   ← test connectivite (Job k8s, pas d'exec)
├── 08-package.sh             ← packaging tarball air-gap
└── RUNBOOK-DGX.md            ← instructions pas a pas
```

### Test de connectivite

Le script `09-connectivity-test.sh` lance un Job ephemere qui teste
11 endpoints (DNS + HTTP + TCP) sans avoir besoin de `kubectl exec`.

Variables configurables :
- `NAMESPACE` (defaut: bootstrap)
- `PROXY` (defaut: proxy DGX)
- `TIMEOUT_SEC` (defaut: 15)
- `TEST_IMAGE` (defaut: postgres:16-alpine)
- `SIM_PROXY_SVC` / `SIM_LLM_SVC` — mode simulation (hostAliases)

Teste valide sur Scaleway avec simulateur proxy (tinyproxy) +
mock LLM API + stubs services — 11/11 passes.

### Packaging air-gap

Le script `08-package.sh` cree une archive autonome contenant :
- Manifests pre-rendus
- Images Docker (docker save)
- Scripts apply/load pour deploiement offline

---

## Instructions de deploiement

Voir `scripts/dgx-deploy/RUNBOOK-DGX.md` pour les instructions
pas a pas a suivre depuis VSCode.

Resume :

```
# 1. Verification locale
kubectl kustomize deploy/k8s/overlays/dgx > /dev/null

# 2. Test connectivite cluster
bash scripts/dgx-deploy/09-connectivity-test.sh

# 3. Deploiement
kubectl apply -k deploy/k8s/overlays/dgx --dry-run=server
kubectl apply -k deploy/k8s/overlays/dgx
kubectl -n bootstrap rollout status deploy/device-management --timeout=180s
kubectl -n bootstrap rollout status deploy/relay-assistant --timeout=180s
kubectl -n bootstrap rollout status deploy/queue-worker --timeout=180s

# 4. Validation
bash scripts/dgx-deploy/09-connectivity-test.sh
```
