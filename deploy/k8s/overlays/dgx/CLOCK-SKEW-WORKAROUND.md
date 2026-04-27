# DGX clock skew — libfaketime workaround

## Contexte

Le node hôte du DGX (`<INTERNAL_DOMAIN>`, single-node k3s) a une
horloge wall-clock qui dérive **en avance** par rapport à UTC. Mesure du
2026-04-27 : **+462 s** (~7 min 42 s).

Cause probable : le réseau du DGX bloque l'egress NTP (UDP/123). Les pools
publics (`pool.ntp.org`, `time.ubuntu.com`) ne sont pas joignables, et le
seul egress disponible est un proxy HTTP (`<PROXY_IP>:3128`) qui ne
relaie pas l'UDP. `chrony` / `systemd-timesyncd` côté hôte ne reçoit donc
plus de référence et la dérive s'accumule librement.

## Pourquoi pas une vraie correction

La correction propre nécessite :

- soit un serveur NTP interne dans `10.0.0.0/8` ou `<INTERNAL_DOMAIN>` ajouté
  dans `/etc/chrony/chrony.conf` du node ;
- soit un saut ponctuel via `chronyc -a makestep` sur le node.

Les deux exigent un **accès SSH au node hôte**, dont nous ne disposons
pas. Pas non plus de contact admin opérant le DGX. La seule surface
d'action est l'API Kubernetes via le token cluster-admin.

Or **Linux ne permet pas de donner à un container une `CLOCK_REALTIME`
différente de l'hôte** : le *time namespace* (kernel ≥ 5.6) ne décale que
`CLOCK_MONOTONIC` / `CLOCK_BOOTTIME`. Aucun `securityContext`, aucune
sysctl ou runtime class n'expose une heure murale par pod.

## Mitigation choisie : `libfaketime` en `LD_PRELOAD`

[libfaketime](https://github.com/wolfcw/libfaketime) intercepte au niveau
libc les appels `time()`, `gettimeofday()`, `clock_gettime()` et applique
un offset configurable. C'est un **pansement applicatif** : le kernel
reste à la mauvaise heure, mais les processus Python voient l'heure
corrigée.

Toutes les apps de `bootstrap/` qui font de la logique métier sont du
**Python (image `device-management`)** → `LD_PRELOAD` fonctionne (libc
dynamique, pas de binaire static-linked).

### Pods couverts par le patch

| Deployment | Container | Image | Couvert |
|---|---|---|---|
| `device-management` | `device-management` | `device-management` (Python) | ✅ oui |
| `device-management-admin` | `device-management-admin` | `device-management` (Python) | ✅ oui |
| `telemetry-relay` | `telemetry-relay` | `device-management` (Python) | ✅ oui |
| `queue-worker` | `queue-worker` | `device-management` (Python) | ✅ oui |
| `relay-assistant` | `relay-assistant` | `nginx:1.27-alpine` | ❌ non — reverse proxy seul, pas de logique liée à l'heure |
| `postgres` | `postgres` | `postgres:16-alpine` | ❌ non — **dangereux** (timestamps de transactions, WAL) |
| `adminer` | `adminer` | `adminer:4.8.1-standalone` | ❌ non — outil debug, hors flux nominal |

### Ce que la mitigation corrige

- `datetime.now(timezone.utc)`, `time.time()`, etc. dans le code Python
- Validation/émission JWT (les tokens auront un `iat` / `exp` corrects vis-à-vis de l'extérieur)
- Validation de certificats TLS côté client Python (`requests`, `httpx`, `urllib3`)
- Timestamps applicatifs écrits en base (mais voir limites ci-dessous)

### Ce que la mitigation NE corrige PAS

- **Timestamps PostgreSQL** : `now()`, `current_timestamp` côté serveur DB
  restent à l'heure faussée du kernel host. Les colonnes `created_at` /
  `updated_at` peuplées par défaut SQL seront en avance de ~462s.
  Les colonnes peuplées par l'app Python seront correctes, ce qui peut
  créer une **incohérence intra-row** entre champs SQL-default et
  champs Python-set.
- **Logs kubelet, events Kubernetes, metrics Prometheus** : à l'heure du
  kernel, donc faux.
- **Mtimes de fichiers** sur les PVC, journald, systemd : faux.
- **Sondes liveness/readiness, leases k8s, certificats kubelet/etcd** : à
  l'heure du kernel.
- **Sockets server-side TLS** terminées par nginx (`relay-assistant`) ou
  Envoy : non patchées.

## Détail technique du patch

Pour chaque deployment Python, le patch :

1. **Monte la ConfigMap `clock-skew-libfaketime`** (volume `libfaketime`) en
   read-only sur `/faketime/`. Cette ConfigMap embarque le binaire
   `libfaketimeMT.so.1` (multi-thread, ~55 KB) directement dans `binaryData`,
   donc aucun téléchargement runtime, aucune dépendance réseau.
2. **Définit quatre variables d'env** sur le container principal :
   - `LD_PRELOAD=/faketime/libfaketime.so.1` — active la lib
   - `FAKETIME=-462` — décale de 462 s vers le passé (corrige le +462 s du host)
   - `FAKETIME_DONT_FAKE_MONOTONIC=1` — laisse `CLOCK_MONOTONIC` intact (asyncio, timeouts, GIL)
   - `FAKETIME_NO_CACHE=1` — pas de cache (le pod peut être rejoué quand la dérive change)

### Pourquoi pas un init container avec `apt install`

Première version essayée. Le proxy DGX (squid « ORION ») exige une auth
Digest/Basic qu'on ne possède pas et renvoie 407 sur HTTP **et** sur HTTPS
(via CONNECT vers `deb.debian.org`). Les pods existants fonctionnent parce
qu'ils ne font pas d'appels publics — ils ne tapent que dans `10.0.0.0/8`
et `.svc.cluster.local`, exclus par `no_proxy`. Pré-staging du `.so` dans
une ConfigMap contourne entièrement le problème.

### Régénérer la ConfigMap

Si une version plus récente de libfaketime est nécessaire :

```bash
# 1. Récupérer le paquet Debian (machine avec accès internet)
curl -sSL -o /tmp/libfaketime.deb \
  http://ftp.debian.org/debian/pool/main/f/faketime/libfaketime_0.9.10-2.1_amd64.deb

# 2. Extraire la lib MT (multi-threaded, recommandée pour Python+asyncio)
mkdir -p /tmp/libfaketime-x && dpkg-deb -x /tmp/libfaketime.deb /tmp/libfaketime-x
SO=/tmp/libfaketime-x/usr/lib/x86_64-linux-gnu/faketime/libfaketimeMT.so.1

# 3. Régénérer le YAML avec base64 inline
B64=$(base64 -w0 "$SO")
# → mettre à jour la valeur `libfaketime.so.1: <B64>` dans
#   clock-skew-libfaketime-configmap.yaml
```

Architecture cible : **amd64 / glibc** (compatible avec `python:3.12-slim`
qui base le Dockerfile `device-management`). Pour de l'ARM ou de la musl,
récupérer la lib correspondante.

## Mettre à jour l'offset

La dérive **augmente avec le temps** (pas de NTP). Re-mesurer
périodiquement et mettre à jour la valeur `FAKETIME` dans les 4 patches.

Mesure (depuis n'importe où avec accès au domaine) :

```bash
LOCAL=$(date -u +%s)
REMOTE=$(date -u -d "$(curl -sk -I https://<INTERNAL_DOMAIN> | grep -i '^date:' | sed 's/^[Dd]ate: //' | tr -d '\r')" +%s)
echo "Offset à appliquer dans FAKETIME : $((LOCAL - REMOTE))"
```

Le résultat est négatif si le DGX est en avance (cas actuel). Reporter
cette valeur dans les 4 fichiers `clock-skew-patch-*.yaml`, puis
redéployer (`kubectl apply -k overlays/dgx`).

Recommandation : re-mesurer et redéployer **toutes les 1 à 2 semaines**
au minimum, ou dès qu'un dysfonctionnement lié à l'heure est observé.

## Retirer le pansement

Quand l'admin du DGX aura corrigé le NTP côté hôte :

1. Supprimer les 4 fichiers `clock-skew-patch-*.yaml`.
2. Retirer leurs entrées de `kustomization.yaml` (section `patches:`).
3. `kubectl apply -k overlays/dgx` pour redéployer sans `LD_PRELOAD`.

**Important** : laisser `FAKETIME` actif après correction NTP côté hôte
recréerait une dérive de 462 s dans l'autre sens. Le retrait doit être
fait dans la même fenêtre que la correction.

## Vérification post-déploiement

Depuis un pod patché (par exemple `device-management`) :

```bash
kubectl -n bootstrap exec deploy/device-management -- python -c \
  "import datetime; print('app sees:', datetime.datetime.now(datetime.timezone.utc).isoformat())"

kubectl -n bootstrap exec deploy/device-management -- date -u
```

Les deux commandes doivent renvoyer **la même heure que `date -u`
local**. Si `date -u` dans le pod renvoie l'heure faussée (en avance),
c'est attendu : `date` est un binaire qui peut bypass `LD_PRELOAD` selon
sa compilation. Le test fiable est celui de Python.

## Historique

- **2026-04-27** : création du pansement, offset mesuré à -462 s.
