# Runbook : Deploiement DGX

## Credentials persistants

Les credentials vivent dans `~/.dm-secrets/` (pas dans le package).
Ils survivent entre les versions du package — on ne les recree jamais.

```
~/.dm-secrets/
├── .env.deploy       ← token DockerHub (DOCKERHUB_USER, DOCKERHUB_TOKEN)
└── .env.secrets      ← secrets applicatifs (passwords, tokens, signing keys)
```

---

## Premier deploiement

```bash
# 1. Extraire le package
tar xzf dgx-deploy-vX.X.tar.gz
cd dgx-deploy-vX.X

# 2. Lancer — le script cree ~/.dm-secrets/ et s'arrete
./dumb-deploy.sh

# 3. Remplir le token DockerHub
nano ~/.dm-secrets/.env.deploy
# DOCKERHUB_USER=etiquet
# DOCKERHUB_TOKEN=dckr_pat_xxxxx

# 4. Remplir les secrets applicatifs
nano ~/.dm-secrets/.env.secrets
# Generer les tokens : python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 5. Relancer — cette fois tout se deploie
./dumb-deploy.sh
```

Ce que fait `dumb-deploy.sh` :
1. Verifie kubectl + cluster joignable
2. Cree le namespace `bootstrap`
3. Cree le secret `regcred` (credentials DockerHub)
4. Cree le secret `device-management-secrets` (si absent — jamais ecrase)
5. Applique les manifests K8s (sans le Secret, gere separement)
6. Bootstrap le schema PostgreSQL (via Job psql, idempotent)
7. Attend les rollouts de tous les Deployments
8. Restart le queue-worker si schema manquait

---

## Redeploiement (mise a jour)

```bash
# Extraire le nouveau package
tar xzf dgx-deploy-vY.Y.tar.gz
cd dgx-deploy-vY.Y

# C'est tout — les credentials sont deja dans ~/.dm-secrets/
./dumb-deploy.sh
```

Les secrets existants dans le cluster ne sont PAS ecrases.

---

## Changer un secret

### Option 1 : via le fichier + redeploy

```bash
# Editer le fichier
nano ~/.dm-secrets/.env.secrets

# Supprimer le secret K8s pour forcer la re-creation
kubectl -n bootstrap delete secret device-management-secrets

# Relancer
./dumb-deploy.sh
```

### Option 2 : patch direct dans le cluster

```bash
kubectl -n bootstrap patch secret device-management-secrets \
  --type=merge -p '{"stringData":{"MA_CLE":"nouvelle-valeur"}}'

# Redemarrer les pods pour prendre en compte
kubectl -n bootstrap rollout restart deploy/device-management
kubectl -n bootstrap rollout restart deploy/device-management-admin
```

---

## Verifier l'etat

```bash
# Fichiers credentials
ls -la ~/.dm-secrets/

# Secret K8s existe ?
kubectl -n bootstrap get secret device-management-secrets

# Pods en cours
kubectl -n bootstrap get pods

# Voir un secret specifique
kubectl -n bootstrap get secret device-management-secrets \
  -o jsonpath='{.data.KEYCLOAK_ISSUER_URL}' | base64 -d; echo

# Dump complet (attention : affiche les valeurs)
kubectl -n bootstrap get secret device-management-secrets -o json | \
  python3 -c "import sys,json,base64; d=json.load(sys.stdin).get('data',{}); \
  [print(f'{k}={base64.b64decode(v).decode()}') for k,v in sorted(d.items())]"
```

---

## Test de connectivite

Verifie que le cluster atteint les endpoints externes via le proxy :

```bash
bash scripts/09-connectivity-test.sh
```

Teste : DNS, SSO mirai, compte-rendu, DockerHub registry, LLM API, services cluster.

---

## Deployer sans tout detruire

```bash
# dumb-deploy.sh est idempotent — il applique les diffs
./dumb-deploy.sh
```

## Deployer from scratch (tout recreer)

```bash
kubectl delete namespace bootstrap --wait=true
./dumb-deploy.sh
```

---

## URLs

| URL | Service |
|-----|---------|
| `https://<DGX_HOSTNAME>/bootstrap/healthz` | API health |
| `https://<DGX_HOSTNAME>/admin/` | Admin UI (SSO) |
| `https://<DGX_HOSTNAME>/catalog` | Catalogue public |
| `https://<DGX_HOSTNAME>/adminer` | DB admin |

---

## Override le repertoire secrets

```bash
DM_SECRETS_DIR=/autre/chemin ./dumb-deploy.sh
```

---

## Depannage

| Symptome | Cause | Action |
|----------|-------|--------|
| `ImagePullBackOff` | Token DockerHub expire | Editer `~/.dm-secrets/.env.deploy` + relancer |
| `CrashLoopBackOff` queue-worker | Schema DB manquant | `./dumb-deploy.sh` (re-applique le schema) |
| 500 sur `/admin/` + `UndefinedTable` | Schema non applique ou DATABASE_ADMIN_URL pointe sur la mauvaise base | `kubectl delete ns bootstrap && ./dumb-deploy.sh` |
| 503 sur `/admin/` | OIDC non configure ou proxy bloque | Verifier logs : `kubectl -n bootstrap logs deploy/device-management-admin --tail=20` |
| 403 sur POST admin | WAF bloque multipart | Fix integre (fetch + JSON). Verifier image version ≥ 0.5.19 |
| 403 sur upload fichier | WAF bloque body > 2MB | Fix integre (chunked upload). Verifier image version ≥ 0.5.17 |
| 407 sur appels inter-pods | Proxy intercepte le trafic interne | Verifier no_proxy inclut les noms courts des services K8s |
| Download "Fichier introuvable" | PVC non monte ou path mismatch | Verifier mount `/data/content/binaries` sur pod device-management |
| Download OK mais fichier vide | Pull-on-miss echoue (token mismatch) | `kubectl -n bootstrap logs deploy/device-management \| grep pull_binary` |
| Tokens `changeme-*` | Ancien .env.secrets | `rm ~/.dm-secrets/.env.secrets && ./dumb-deploy.sh` (auto-genere) |
| `dumb-deploy.sh` demande les creds | Premier lancement | Remplir `~/.dm-secrets/.env.deploy` seulement (secrets auto-generes) |

## Architecture des pods

```
                            ┌─ device-management (API)
                            │  PVC: /data/content/binaries + /data/enroll
Navigateur → WAF → Envoy ──┤
                            ├─ device-management-admin
                            │  PVC: /data/content (binaires + uploads)
                            │
                            ├─ relay-assistant (nginx proxy)
                            ├─ queue-worker (jobs async)
                            ├─ telemetry-relay
                            └─ postgres (PVC: /var/lib/postgresql/data)

Pull-on-miss: API pod tire les binaires de admin pod via HTTP
              (DM_QUEUE_ADMIN_TOKEN doit matcher entre les 2 pods)
```
