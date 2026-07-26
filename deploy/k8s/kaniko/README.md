# Build de l'image — deux options

| | `scripts/build-k8s.sh` (buildx) | `scripts/build-kaniko.sh` (in-cluster) |
|---|---|---|
| Où tourne le build | poste de dev | dans le cluster cible |
| Prérequis | Docker + buildx | un kubeconfig + un secret de push |
| Source construite | votre copie de travail | le commit **poussé** sur origin |
| Architectures | `amd64` + `arm64` (manifeste multi-arch) | celle des nœuds (**mono-arch**) |
| Réseau | l'image transite par votre poste | push direct cluster → registre |

**Choisir buildx** pour une release (le multi-arch est nécessaire dès qu'une
cible n'est pas `amd64`), **choisir Kaniko** quand on n'a pas Docker sous la
main, quand le lien du poste est le goulot, ou pour construire depuis un runner
CI qui ne dispose que d'un kubeconfig.

## Utilisation

```bash
# Intégration : tag = VERSION, branche courante, ns bootstrap
DM_REGISTRY_OVERRIDE=docker.io/<namespace> ./scripts/build-kaniko.sh

# Tag explicite, sans toucher au tag `latest`, avec cache de couches
DM_REGISTRY_OVERRIDE=docker.io/<namespace> \
  ./scripts/build-kaniko.sh 0.9.15 --no-latest --cache

# Voir le Job sans rien appliquer
./scripts/build-kaniko.sh --dry-run
```

Le script lit `.env.registry` (gitignoré, cf. `.env.registry.example`) comme
`build-k8s.sh`, et accepte `--namespace` / `--kube-context` / `--push-secret`.
`kaniko-build-job.yaml` est le gabarit appliqué ; il reste du YAML valide et
peut être substitué à la main pour un `kubectl apply` sans le script.

## Garde importante

Kaniko construit **depuis git**, pas depuis votre copie de travail. Le script
refuse donc de partir si le HEAD local n'est pas celui d'`origin/<branche>` —
sinon vous croiriez construire vos modifications alors que le cluster
construirait le commit distant. `--allow-unpushed` lève la garde (utile pour
reconstruire une réf ancienne).

## Prérequis dans le namespace

Un secret `kubernetes.io/dockerconfigjson` **avec droits de push** sur le dépôt
d'images. Par défaut le script réutilise le pull secret (`regcred`) : si son
token est en lecture seule, créez-en un dédié et passez `--push-secret`.

## Découpage DM / DM-private

Le **mécanisme** de build vit ici, avec le `Dockerfile` qu'il construit et sans
aucune valeur d'environnement en dur (registre, namespace, cluster viennent de
`.env.registry`, des options, ou de l'environnement). Le **ciblage** d'un
environnement donné — `DM_REGISTRY_OVERRIDE`, contexte kubectl, namespace —
appartient au dépôt de déploiement `device-management-private`, à côté des
autres runners (`force-redeploy.sh`, `freshstart.sh`, `site.*.env`).
