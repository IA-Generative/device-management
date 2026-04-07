# Runbook : Deploiement DGX depuis VSCode distant

## Ce qui a change

Les modifications suivantes sont deja appliquees aux fichiers YAML du repo :

**Proxy corporate sur tous les pods sortants :**
- `deploy/k8s/overlays/dgx/proxy-patch-device-management.yaml` (nouveau)
- `deploy/k8s/overlays/dgx/proxy-patch-relay-assistant.yaml` (nouveau)
- `deploy/k8s/overlays/dgx/proxy-patch-queue-worker.yaml` (nouveau)
- `deploy/k8s/overlays/dgx/kustomization.yaml` (modifie — 3 targets proxy)
- L'ancien `proxy-patch.yaml` (qui ne ciblait que device-management) est remplace

**Nouveau relay `compte-rendu` :**
- `deploy/k8s/base/manifests/25-relay-assistant-configmap.yaml` — bloc nginx `/compte-rendu/`
- `deploy/k8s/base/manifests/26-relay-assistant-deployment.yaml` — env `RELAY_COMPTE_RENDU_UPSTREAM`
- `deploy/k8s/base/secrets/all-secrets.yaml` — placeholder

**Secrets DGX :**
- `deploy/k8s/overlays/dgx/secret-patch.yaml` :
  - `RELAY_KEYCLOAK_UPSTREAM` → `https://<SSO_HOSTNAME>/realms/mirai`
  - `RELAY_COMPTE_RENDU_UPSTREAM` → `https://<COMPTERENDU_HOSTNAME>`
  - `DM_RELAY_ALLOWED_TARGETS_CSV` → `keycloak,config,llm,mcr-api,telemetry,compte-rendu`
  - `DM_AUTH_JWKS_URL` → `https://<SSO_HOSTNAME>/realms/mirai/protocol/openid-connect/certs`

---

## Pre-requis

### Sur ta machine (VSCode)

```
kubectl      → configure pour pointer vers le cluster DGX
kustomize    → inclus dans kubectl (v1.27+)
ssh          → acces au noeud DGX (si besoin transfert images)
```

Verifier l'acces cluster :

```bash
kubectl cluster-info
kubectl get nodes
kubectl -n bootstrap get pods
```

Si le kubeconfig n'est pas le defaut :

```bash
export KUBECONFIG=~/.kube/dgx-config
```

---

## Phase 1 — Verification locale (VSCode, pas de contact cluster)

### 1.1 Verifier que kustomize rend correctement

```bash
kubectl kustomize deploy/k8s/overlays/dgx > /dev/null && echo "OK" || echo "FAIL"
```

### 1.2 Verifier le proxy sur les 3 deployments

```bash
RENDERED=$(kubectl kustomize deploy/k8s/overlays/dgx)
for deploy in device-management relay-assistant queue-worker; do
  echo -n "$deploy proxy: "
  echo "$RENDERED" | grep -A60 "name: $deploy" | grep -q 'proxydc-sir' && echo "OK" || echo "MISSING"
done
```

Resultat attendu :

```
device-management proxy: OK
relay-assistant proxy: OK
queue-worker proxy: OK
```

### 1.3 Verifier le relay compte-rendu dans nginx

```bash
kubectl kustomize deploy/k8s/overlays/dgx | grep 'relay_target compte-rendu'
```

Doit afficher :

```
        set $relay_target compte-rendu;
```

### 1.4 Verifier les secrets DGX

```bash
kubectl kustomize deploy/k8s/overlays/dgx | grep -E 'RELAY_COMPTE_RENDU|RELAY_KEYCLOAK|DM_RELAY_ALLOWED'
```

Doit afficher les 3 variables avec les bonnes valeurs.

### 1.5 Verifier que no_proxy ne contient PAS .gouv.fr

```bash
kubectl kustomize deploy/k8s/overlays/dgx | grep 'no_proxy' | head -1
```

Le domaine `.gouv.fr` ne doit PAS apparaitre dans la valeur
(sinon le SSO et compte-rendu bypasseraient le proxy).

### 1.6 Diff visuel (optionnel)

```bash
kubectl kustomize deploy/k8s/overlays/dgx > /tmp/dgx-rendered.yaml
wc -l /tmp/dgx-rendered.yaml
grep -n 'compte-rendu' /tmp/dgx-rendered.yaml
grep -n 'RELAY_.*UPSTREAM' /tmp/dgx-rendered.yaml
```

---

## Phase 2 — Test connectivite (sur le cluster DGX)

Avant de deployer, on verifie que le cluster atteint les endpoints
externes. Le script utilise un **Job K8s ephemere** (pas besoin de
`kubectl exec`).

### 2.1 Lancer le test

```bash
bash scripts/dgx-deploy/09-connectivity-test.sh
```

Le script :
1. Verifie le secret `regcred` (existe, cible `rg.fr-par.scw.cloud`)
2. Lance un Job avec l'image `postgres:16-alpine` (deja dans le cluster)
3. Teste DNS, HTTP via proxy, HTTP direct, services cluster, registry
4. Lit les resultats via `kubectl logs`
5. Supprime le Job automatiquement

### 2.2 Endpoints testes

| Endpoint | Type | Attendu |
|----------|------|---------|
| <SSO_HOSTNAME> (OIDC) | via proxy | HTTP < 500 |
| <SSO_HOSTNAME> (JWKS) | via proxy | HTTP < 500 |
| <COMPTERENDU_HOSTNAME> | via proxy | HTTP < 500 |
| rg.fr-par.scw.cloud/v2/ (registry) | via proxy | HTTP < 500 (301 ou 401 = OK) |
| <LLM_API_HOSTNAME>/v1/models | direct (<INTERNAL_DOMAIN>) | HTTP < 500 |
| device-management:3001/health | cluster interne | HTTP 200 |
| relay-assistant:8080/healthz | cluster interne | HTTP 200 |
| relay-assistant JWKS passthrough | cluster interne | JSON "keys" |
| postgres:5432 | cluster TCP | pg_isready |
| Registry v2 API | via proxy | HTTP < 500 |
| Image path | via proxy | HTTP < 500 |

Pre-flight (hors Job) :

| Test | Attendu |
|------|---------|
| Secret `regcred` existe | present dans namespace |
| `regcred` cible `rg.fr-par.scw.cloud` | serveur correct |
| Deployments ont `imagePullSecrets` | `regcred` reference |

Resultat attendu :

```
Results: 11 passed, 0 failed, 0 warnings
```

> **Note :** HTTP 301 ou 401 pour la registry = OK.
> Ca prouve que le reseau passe. Le pull reel utilise le secret `regcred`.

### 2.3 En cas d'echec connectivite

**DNS echoue :**
→ Verifier CoreDNS : `kubectl -n kube-system get cm coredns -o yaml`
→ Ajouter un forward vers le DNS corporate si besoin

**Proxy echoue :**
→ Verifier que `<PROXY_HOSTNAME>:3128` est joignable depuis les pods
→ Verifier les NetworkPolicy

**Service cluster echoue :**
→ Normal si c'est le premier deploiement (les services n'existent pas encore)
→ Refaire le test apres le deploy (phase 4)

---

## Phase 3 — Deploiement (sur le cluster DGX)

### 3.1 Dry-run serveur

```bash
kubectl apply -k deploy/k8s/overlays/dgx --dry-run=server
```

Si OK, continuer. Si erreur, corriger avant d'appliquer.

### 3.2 Appliquer

```bash
kubectl apply -k deploy/k8s/overlays/dgx
```

### 3.3 Surveiller les rollouts

```bash
kubectl -n bootstrap rollout status deploy/device-management --timeout=180s
kubectl -n bootstrap rollout status deploy/relay-assistant --timeout=180s
kubectl -n bootstrap rollout status deploy/queue-worker --timeout=180s
```

### 3.4 Verifier les pods

```bash
kubectl -n bootstrap get pods -o wide
```

Tous les pods doivent etre `Running` et `READY`.

Si un pod est en `CrashLoopBackOff` :

```bash
kubectl -n bootstrap logs deploy/<nom> --tail=50
kubectl -n bootstrap describe pod <nom-du-pod>
```

### 3.5 Verifier le proxy dans les pods

```bash
for deploy in device-management relay-assistant queue-worker; do
  echo -n "$deploy: "
  kubectl -n bootstrap get deploy "$deploy" -o jsonpath='{.spec.template.spec.containers[0].env}' | grep -q 'proxydc-sir' && echo "OK" || echo "MISSING"
done
```

### 3.6 Re-tester la connectivite complete

```bash
bash scripts/dgx-deploy/09-connectivity-test.sh
```

Cette fois tous les services cluster doivent aussi passer.

---

## Phase 4 — Packaging air-gap (optionnel)

Si tu dois transferer le deploiement sur un DGX deconnecte :

```bash
bash scripts/dgx-deploy/08-package.sh v1.0
```

Produit : `dist/dgx-deploy-v1.0.tar.gz`

Contenu :
- `manifests/dgx-all.yaml` — manifests pre-rendus
- `images/*.tar` — images Docker (docker save)
- `apply.sh` — deploiement autonome
- `load-images.sh` — charge les images dans containerd
- `scripts/` — scripts de validation

Sur le DGX cible :

```bash
tar xzf dgx-deploy-v1.0.tar.gz
cd dgx-deploy-v1.0
./load-images.sh
./apply.sh
```

---

## Aide-memoire rapide

```
# ---- Verification locale ----
kubectl kustomize deploy/k8s/overlays/dgx > /dev/null   # kustomize OK ?

# ---- Test connectivite ----
bash scripts/dgx-deploy/09-connectivity-test.sh          # 11 tests

# ---- Deploiement ----
kubectl apply -k deploy/k8s/overlays/dgx --dry-run=server
kubectl apply -k deploy/k8s/overlays/dgx
kubectl -n bootstrap rollout status deploy/device-management --timeout=180s
kubectl -n bootstrap rollout status deploy/relay-assistant --timeout=180s
kubectl -n bootstrap rollout status deploy/queue-worker --timeout=180s

# ---- Validation post-deploy ----
kubectl -n bootstrap get pods -o wide
bash scripts/dgx-deploy/09-connectivity-test.sh          # re-test complet

# ---- Packaging (optionnel) ----
bash scripts/dgx-deploy/08-package.sh v1.0
```

---

## Rollback

```bash
# Voir l'historique
kubectl -n bootstrap rollout history deploy/device-management
kubectl -n bootstrap rollout history deploy/relay-assistant

# Revenir a la version precedente
kubectl -n bootstrap rollout undo deploy/device-management
kubectl -n bootstrap rollout undo deploy/relay-assistant
kubectl -n bootstrap rollout undo deploy/queue-worker
```

---

## Depannage

| Symptome | Cause probable | Action |
|----------|---------------|--------|
| Pod `ImagePullBackOff` | Registry injoignable ou creds invalides | `09-connectivity-test.sh` + recreer `regcred` |
| Pod `ImagePullBackOff` + `401` | Secret `regcred` manquant/expire | `./scripts/k8s/create-registry-secret.sh dgx` |
| Pod `ImagePullBackOff` + `timeout` | Proxy bloque `rg.fr-par.scw.cloud` | Verifier whitelist proxy corporate |
| Pod `ErrImagePull` + TLS | Proxy MITM le TLS | Ajouter CA corporate ou bypass TLS |
| Pod `CrashLoopBackOff` | Config/secret manquant | `kubectl logs` + verifier secret-patch |
| relay-assistant 502 | Upstream injoignable | `09-connectivity-test.sh` |
| SSO timeout | Proxy manquant sur relay-assistant | Verifier proxy-patch-relay-assistant.yaml |
| DNS fail | CoreDNS ne forward pas | Verifier configmap `coredns` dans kube-system |
| `compte-rendu` 403 | Target pas dans allowed list | Verifier `DM_RELAY_ALLOWED_TARGETS_CSV` |
| `no_proxy` bypass SSO | `.gouv.fr` dans no_proxy | Ne PAS ajouter `.gouv.fr` dans no_proxy |
