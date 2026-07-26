# Build de l'image — deux voies, un seul moteur

Les deux voies utilisent **BuildKit** : `docker buildx` en est le client local,
`buildctl` le client in-cluster. Les images produites sont donc équivalentes par
construction, pas par espoir.

| | `scripts/build-k8s.sh` (buildx) | `scripts/build-incluster.sh` (BuildKit rootless) |
|---|---|---|
| Où tourne le build | poste de dev | dans le cluster cible |
| Prérequis | Docker + buildx | un kubeconfig + un secret de push |
| Source construite | votre copie de travail | le commit **poussé** sur origin |
| Architectures | `amd64` + `arm64` | celle des nœuds par défaut ; multi-arch possible (voir plus bas) |
| Réseau | l'image transite par votre poste | push direct cluster → registre |
| Privilèges | démon Docker local | uid 1000, **sans root ni privilège** |

**Choisir buildx** pour une release multi-arch depuis un poste équipé.
**Choisir le build in-cluster** quand on n'a pas Docker sous la main, quand le
lien du poste est le goulot, ou pour un runner CI qui ne dispose que d'un
kubeconfig.

## Utilisation

```bash
# Intégration : tag = VERSION, branche courante, ns bootstrap
DM_REGISTRY_OVERRIDE=docker.io/<namespace> ./scripts/build-incluster.sh

# Tag explicite, sans toucher au tag `latest`, avec cache de couches au registre
DM_REGISTRY_OVERRIDE=docker.io/<namespace> \
  ./scripts/build-incluster.sh 0.9.15 --no-latest --cache

# Voir le Job sans rien appliquer
./scripts/build-incluster.sh --dry-run
```

Le script lit `.env.registry` (gitignoré, cf. `.env.registry.example`) comme
`build-k8s.sh`, et accepte `--namespace` / `--kube-context` / `--push-secret` /
`--platforms`. `buildkit-build-job.yaml` est le gabarit appliqué ; il reste du
YAML valide et peut être substitué à la main pour un `kubectl apply` sans le
script.

## Multi-architecture

BuildKit sait émettre un manifeste multi-plateformes in-cluster
(`--platforms linux/amd64,linux/arm64`), **mais** construire une architecture
étrangère à celle des nœuds exige que QEMU/binfmt soit enregistré sur l'hôte —
typiquement un DaemonSet `tonistiigi/binfmt`. Sans cela, l'étape `arm64` échoue.

Le défaut est donc `linux/amd64`, l'architecture des nœuds de l'intégration.
Élargissez une fois binfmt en place ; sinon les releases multi-arch restent le
domaine de `build-k8s.sh` depuis un poste.

## Garde importante

Le build part de **git**, pas de votre copie de travail. Le script refuse donc
de partir si le HEAD local n'est pas celui d'`origin/<branche>` — sinon vous
croiriez construire vos modifications alors que le cluster construirait le
commit distant. `--allow-unpushed` lève la garde (utile pour reconstruire une
réf ancienne).

## Prérequis dans le namespace

Un secret `kubernetes.io/dockerconfigjson` **avec droits de push** sur le dépôt
d'images. Par défaut le script réutilise le pull secret (`regcred`) : si son
token est en lecture seule, créez-en un dédié et passez `--push-secret`.

Le pod tourne en uid 1000, sans privilège, sans jeton de ServiceAccount. Il
réclame en revanche `seccomp` et `AppArmor` en `Unconfined` : BuildKit rootless
crée des espaces de noms utilisateur imbriqués que les profils par défaut
interdisent. C'est strictement moins de droits que ce qu'exigeait Kaniko (root).

## Historique

Cette voie reposait sur **Kaniko** jusqu'au 2026-07-26. Le projet
`GoogleContainerTools/kaniko` a été **archivé** en 2025 : plus aucun correctif,
y compris de sécurité. Le passage à BuildKit a en outre levé trois limites — un
moteur différent de celui des releases, l'impossibilité du multi-arch, et
l'obligation de tourner en root.

## Découpage DM / DM-private

Le **mécanisme** de build vit ici, avec le `Dockerfile` qu'il construit et sans
aucune valeur d'environnement en dur (registre, namespace, cluster viennent de
`.env.registry`, des options, ou de l'environnement). Le **ciblage** d'un
environnement donné — `DM_REGISTRY_OVERRIDE`, contexte kubectl, namespace —
appartient au dépôt de déploiement `device-management-private`, à côté des
autres runners (`force-redeploy.sh`, `freshstart.sh`, `site.*.env`).
