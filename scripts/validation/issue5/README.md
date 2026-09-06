# Banc de validation — issue #5 (cache binaire non invalidé au ré-upload)

Le défaut ne se manifeste que si **l'upload et le service écrivent sur deux
disques différents** — la topologie de production : le pod *admin* porte le
volume où l'upload atterrit, chaque pod *API* a son propre cache alimenté par
pull-on-miss. Le `docker-compose.yml` du dépôt n'a qu'un conteneur : l'upload y
écrase le cache qu'il sert, et **le bug ne peut pas s'y reproduire**. D'où ce banc.

Réglage indispensable dans les deux cas : **`DM_BINARIES_MODE=local`**. En
`presign`/`proxy` le binaire est servi depuis S3 sans jamais toucher au disque,
et le scénario passe sans rien montrer.

## En local (Docker) — deux conteneurs, deux volumes

```bash
docker compose -f docker-compose.repro.yml up -d --build
```
`admin` (port 18091, `DM_RUNTIME_MODE=all`) reçoit les publications ; `api`
(port 18089) sert les téléchargements et cache dans son propre volume.

## Sur Kubernetes — namespace dédié

Ne **jamais** jouer ce banc dans le namespace `bootstrap` : c'est la plateforme
d'intégration. `banc.yaml` est un gabarit : substituer `__IMAGE__` (l'image à
éprouver) et `__TOKEN__` (un jeton d'admin jetable, généré localement).

```bash
kubectl create namespace dm-issue5-test
sed -e "s|__IMAGE__|docker.io/<ns>/device-management:<tag>|" \
    -e "s|__TOKEN__|$(openssl rand -hex 24)|" banc.yaml | kubectl apply -f -
```

Les sondes tournent en **Job** dans le namespace (l'API de certains clusters
refuse `kubectl exec` — cf. `IA-Generative/platform-team#259`) ; on relit leur
sortie avec `kubectl logs job/<nom>`. Monter le script par ConfigMap :

```bash
kubectl -n dm-issue5-test create configmap scenario --from-file=scenario.py
```

| Sonde | Ce qu'elle établit |
|---|---|
| `scenario.py` | le scénario de l'issue : publier A, télécharger, republier B **sous le même numéro**, retélécharger. Avant le correctif → `CHECKSUM MISMATCH` ; après → concordance. |
| `carryover.py` | un cache **déjà** périmé, hérité du PVC avant la montée de version, est réparé au premier téléchargement — sans geste d'exploitation. |
| `multi_replicas.py` | deux répliques d'API aux caches indépendants se réparent **chacune seule** : c'est l'argument qui a fait préférer la vérification au service à une invalidation distribuée. |
| `concordance.py` | vue **client**, sans aucun secret : interroge `/config/config.json` pour le checksum promis, télécharge le binaire annoncé et compare. Se lance de l'extérieur contre n'importe quelle plateforme. |

`concordance.py` n'écrit rien : il n'envoie pas de `X-Client-UUID`, donc aucune
ligne `campaign_device_status` (l'upsert y est conditionné dans `main.py`).

```bash
python3 concordance.py https://<hôte-public>
```
