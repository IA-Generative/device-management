# Capitalisation — Déploiement DGX

Journal des sessions de travail, valeurs locales en cours d'expérimentation, décisions à reprendre. Chaque entrée est datée. Les valeurs validées migrent ensuite dans le RUNBOOK ou la config versionnée.

---

## Session du 2026-04-27

### État du repo en début de session

- Branche `main` en retard de 1 commit sur `origin/main` (fast-forward possible).
- Commit distant à intégrer : `b685e8a` — *fix(dgx): replace all placeholders with real DGX values* (overlay k8s `deploy/k8s/overlays/dgx/` : httproute, 4 proxy-patch, env-secrets).
- Modification locale non commitée dans `scripts/dgx-deploy/.env.config`.

### Valeur locale en cours de test

Fichier : `scripts/dgx-deploy/.env.config`

```diff
+DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS=true
```

Ajouté localement (non poussé). Mis de côté via `git stash` avant le pull du commit distant pour avoir un working tree propre. À valider/commiter après réintégration.

À documenter une fois la valeur validée :
- Effet exact du flag `DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS`
- Composant qui le lit (relay ? auth ?)
- Conditions dans lesquelles il doit être à `true` sur DGX

### Actions

- [x] Stash de la modif locale (`stash@{0}`)
- [x] `git pull --ff-only` → `b685e8a` intégré
- [ ] `git stash pop` pour réappliquer la modif locale
- [ ] Valider et commiter la valeur si le test est concluant

### Bascule registry Scaleway → DockerHub (alignement avec DGX)

**Contexte** : sur Scaleway, les pods tournaient en `0.5.15` depuis le registry Scaleway interne `rg.fr-par.scw.cloud/funcscw…`, alors que DGX était déjà à `0.5.23` sur DockerHub `etiquet/*`. Pour aligner les versions et n'avoir qu'un seul pipeline de publication, bascule de Scaleway sur DockerHub.

**Modifications** :

- [`.env.registry.dockerhub`](../../.env.registry.dockerhub) — nouveau fichier (gitignoré) avec credentials DockerHub `etiquet`
- [`deploy/k8s/overlays/scaleway/kustomization.yaml`](../../deploy/k8s/overlays/scaleway/kustomization.yaml) — ajout d'un bloc `images:` qui remappe `<SCALEWAY_REGISTRY>/device-management` vers `docker.io/etiquet/device-management:0.5.23` (et idem pour postgres, nginx, adminer)
- [`deploy/k8s/overlays/scaleway/hpa-patch.yaml`](../../deploy/k8s/overlays/scaleway/hpa-patch.yaml) — nouveau fichier : HPA `device-management` forcé à `min=1, max=1`
- Ajout d'un bloc `replicas:` dans le kustomization pour `device-management count: 1`

**Commandes exécutées (cluster Scaleway prod, namespace `bootstrap`)** :

```bash
REGISTRY_ENV_FILE=.env.registry.dockerhub scripts/k8s/create-registry-secret.sh scaleway
kubectl apply -k deploy/k8s/overlays/scaleway/
kubectl -n bootstrap patch hpa device-management --patch '{"spec":{"minReplicas":1,"maxReplicas":1}}'
kubectl -n bootstrap scale deployment device-management --replicas=1
```

### ⚠️ Leçon clé : PVCs ReadWriteOnce + multi-replicas = piège

Le déploiement initial avait `replicas: 4` pour `device-management` (HPA `min=4, max=20`), mais les PVCs `device-management-content-pvc` et `device-management-enroll-pvc` sont en **`ReadWriteOnce`** sur `sbs-default` (Scaleway Block Storage).

**Pourquoi ça "marchait"** : avec inertie historique, des pods sur des nœuds différents tournaient depuis 25 jours sans redémarrage. Le CSI Scaleway tolère probablement un état dégradé tant qu'aucun nouvel attach n'est demandé.

**Pourquoi ça a cassé pendant le rollout** : le rolling update force des nouveaux attaches CSI. RWO refuse → 4 pods coincés en `ContainerCreating` avec `Multi-Attach error`.

**Mitigation appliquée (option A)** : passer à 1 replica (modèle DGX). Pérennisé via `replicas:` + `hpa-patch.yaml` dans l'overlay.

**Pistes long terme si besoin de redondance** :
- Migrer `content` et `enroll` sur S3 (le code supporte `DM_BINARIES_MODE=s3` et `DM_STORE_ENROLL_S3=true`)
- Ou changer de StorageClass pour un mode RWX (NFS Scaleway)

### État final post-bascule

| Composant | Image (DockerHub) | Replicas |
|---|---|---|
| `device-management` | `etiquet/device-management:0.5.23` | 1/1 |
| `device-management-admin` | `etiquet/device-management:0.5.23` | 1/1 |
| `queue-worker` | `etiquet/device-management:0.5.23` | 2/2 |
| `telemetry-relay` | `etiquet/device-management:0.5.23` | 1/1 |
| `postgres` | `etiquet/postgres:16-alpine` | 1/1 |
| `relay-assistant` | `etiquet/nginx:1.27-alpine` | 1/1 |
| `adminer` | `etiquet/adminer:4.8.1-standalone` | 1/1 |

Aucun pod ancien restant, secret `regcred` pointe sur `https://index.docker.io/v1/`.

### Notes pour la prochaine session

- Les Ingress `bootstrap-extras` et `device-management-ingress` contiennent des placeholders `<SCALEWAY_HOSTNAME>` dans le repo (anonymisés) — l'apply les rejette avec une erreur RFC1123 mais les ressources existantes restent intactes côté cluster. À traiter via un `ingress-patch.yaml` dans l'overlay scaleway pour rendre l'apply propre, ou via une approche templating (envsubst).
- Pod `stress-test-300k-m88f9` Completed depuis 29 jours, traîne dans `bootstrap`. À nettoyer.
- À surveiller : pull rate limit DockerHub. Token authentifié `etiquet` → 200 pulls / 6h. Devrait suffire mais à monitorer si rollouts fréquents.
