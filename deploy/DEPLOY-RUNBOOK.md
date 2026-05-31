# Runbook de déploiement — release DM-1→4 + normalisation secrets

Point de sortie unique pour déployer tout le travail en stock sur **scaleway** et **dgx**,
et le tester. Travail livré dans 2 PR (#6 migration, #7 secrets ; #7 contient #6).

## 0. Pré-requis & ordre de merge
1. Merger **#6** (migration DM-1→4) puis **#7** (normalisation secrets) sur `main`
   (ou merger #7 seule : elle contient les 10 commits). Vérifier `kubectl kustomize` OK.
2. Avoir le contexte kubectl de l'env cible :
   - scaleway → `<K8S_CONTEXT>`, ns `bootstrap`
   - dgx → contexte DGX, ns `bootstrap`

## 1. Build & push de l'image (le code neuf ne tourne pas sans ça)
Le pod tourne `0.5.23` ; le code (routes `/updates`, upload multi-variant, fixups DB) exige
une nouvelle image.
```
cp .env.registry.example .env.registry   # renseigner REGISTRY_SERVER si absent
./scripts/build-k8s.sh 0.6.0              # build multi-arch + push
```
Puis bumper le tag dans l'overlay cible : `overlays/<env>/kustomization.yaml` → `newTag: "0.6.0"`.

## 2. Secrets (cf. NORMALIZATION-secrets.md) — AVANT l'apply
La base ne fournit plus les secrets. Pour l'env cible :
```
cd deploy/k8s/overlays/<env>
# si un ancien fichier existe : mv secret-patch.yaml env-secrets.yaml
cp env-secrets.yaml.example env-secrets.yaml   # si absent
```
Renseigner dans `env-secrets.yaml` **tous** les `<CHANGEME>` avec les **valeurs actuelles**
(lues depuis le secret live) :
```
kubectl -n bootstrap get secret device-management-secrets -o jsonpath='{.data.LLM_API_TOKEN}' | base64 -d
```
(idem pour DM_QUEUE_ADMIN_TOKEN, DM_TELEMETRY_UPSTREAM_KEY, DM_TELEMETRY_TOKEN_SIGNING_KEY,
DM_RELAY_PROXY_SHARED_TOKEN, DM_RELAY_SECRET_PEPPER, POSTGRES_PASSWORD, DATABASE_URL,
DATABASE_ADMIN_URL, TELEMETRY_KEY/SALT + 6 tokens DM-2). Vérifier : aucun `<CHANGEME>` résiduel
dans `kubectl kustomize overlays/<env>`.
> Rotation recommandée (exposés en historique) : LLM_API_TOKEN, DM_TELEMETRY_UPSTREAM_KEY,
> DM_TELEMETRY_TOKEN_SIGNING_KEY. POSTGRES_PASSWORD : reprendre la valeur existante.

## 3. Renseigner les valeurs DM-2 (tokens extension)
Dans `env-secrets.yaml`, poser les vraies valeurs de `API_BASE`, `RELAY_ASSISTANT_BASE_URL`,
`COMPTE_RENDU_URL`, `COMU_URL`, `TELEMETRY_ENDPOINT`, `TELEMETRY_KEY` (sinon ces tokens sortent
vides dans config.json). Pour DM-1, `KEYCLOAK_REDIRECT_URI`/`ALLOWED` doivent être l'URL de
callback publique (cf. valeurs déjà posées : `https://<INTERNAL_DOMAIN>/callback`).

## 4. Apply
```
kubectl apply -k deploy/k8s/overlays/<env>
kubectl -n bootstrap rollout status deploy/device-management --timeout=180s
```
> ⚠️ Si l'overlay contient des placeholders d'infra (`<SCALEWAY_HOSTNAME>` dans l'ingress, etc.),
> les renseigner d'abord (checkout réel non sanitizé). Sur scaleway, un changement *de secret seul*
> peut aussi se faire par patch ciblé (cf. memory project_scaleway_deploy).

## 5. Migrations DB — automatiques au démarrage
`apply_schema` (startup) crée `plugin_version_artifacts` (CREATE TABLE IF NOT EXISTS) et ajoute
`plugins.extension_id`/`gecko_id` (fixup ALTER). **Rien à lancer manuellement.** Vérif :
```
kubectl -n bootstrap exec deploy/postgres -- psql -U postgres -d bootstrap -tAc \
 "SELECT count(*) FROM information_schema.columns WHERE table_name='plugins' AND column_name IN ('extension_id','gecko_id')"   # => 2
```

## 6. Bascule slug (quand la release client est prête)
```
kubectl -n bootstrap exec -i deploy/postgres -- psql -U postgres -d bootstrap < scripts/dm4-slug-migration.sql
```
Pose extension_id/gecko_id, renomme le slug, crée l'alias `mirai-browser`, l'override
`keycloakClientId=mirai-extension`. Idempotent.

## 7. Smoke tests (in-cluster, sans exposer le host)
```
POD=$(kubectl -n bootstrap get pod -l app=device-management -o jsonpath='{.items[0].metadata.name}')
# DM-1 : redirect_uri non vide (flux LibreOffice)
kubectl -n bootstrap exec "$POD" -- curl -s 'http://localhost:3001/config/mirai-libreoffice/config.json?profile=prod' | grep -o 'keycloak_redirect_uri[^,]*'
# DM-2 : 9 tokens substitués (extension)
kubectl -n bootstrap exec "$POD" -- curl -s 'http://localhost:3001/config/iassistant-direct-browser/config.json?profile=prod'
# DM-4 : manifests (après bascule + au moins une release uploadée)
kubectl -n bootstrap exec "$POD" -- curl -s 'http://localhost:3001/updates/iassistant-direct-browser/scaleway.xml'   # appid=cjaokgc…
kubectl -n bootstrap exec "$POD" -- curl -s 'http://localhost:3001/updates/mirai-browser/scaleway.xml'                # alias dual-serve
```
Cibles : `<target>` = `scaleway` ou `dgx` selon l'env. (Tests live externes : `tests/test_post_deploy.py`
avec `DM_BASE_URL=https://<host>`.)

## 8. Rollback
- Image : `newTag` précédent + `kubectl apply -k` (ou `rollout undo`).
- Secret : re-patch / ré-apply de l'`env-secrets.yaml` précédent.
- DB : migrations additives (colonnes/table en plus) — pas de rollback requis ; bloc de rollback
  data dans `scripts/dm4-slug-migration.sql`.

## Points ouverts (n'empêchent pas le déploiement DM-1/DM-2)
- Clé de config exacte du `client_id` côté template client (override `keycloakClientId`).
- Contrat `versions/upload` par `(format,target)` + valeurs réelles `COMU_URL`/`API_BASE`/`RELAY_ASSISTANT_BASE_URL` (ops).
- Whitelist Keycloak `bootstrap-iassistant` + `mirai-extension`.
