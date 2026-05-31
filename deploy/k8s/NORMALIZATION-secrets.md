# Normalisation de la gestion des secrets (k8s)

## Convention (cible, tous environnements)

- Les **manifests versionnés ne contiennent AUCUN secret réel**.
- `base/secrets/all-secrets.yaml` (versionné) = config **non sensible** + placeholders
  `<CHANGEME>` pour toute clé secrète.
- Chaque overlay a **un** `secret-patch.yaml` **gitignored** (valeurs réelles, posées
  **après création du cluster**) et **un** `secret-patch.yaml.example` versionné (gabarit).
- `.gitignore` : `deploy/k8s/overlays/*/secret-patch.yaml` (glob, couvre tout overlay,
  y compris `prod-beta`), `!*.example`.

## État après ce refactor

| Env | secret-patch.yaml | .example versionné |
|-----|-------------------|--------------------|
| local | gitignored (défauts dev fournis dans l'.example) | ✅ |
| scaleway | gitignored | ✅ |
| dgx | gitignored | ✅ |
| prod-beta | gitignored (à créer) | ✅ (squelette) |

La base ne fournissant plus les secrets partagés, **chaque overlay doit désormais les
fournir** : `LLM_API_TOKEN`, `DM_QUEUE_ADMIN_TOKEN`, `DM_TELEMETRY_UPSTREAM_KEY`,
`DM_TELEMETRY_TOKEN_SIGNING_KEY`, `DM_RELAY_PROXY_SHARED_TOKEN`, `DM_RELAY_SECRET_PEPPER`,
`POSTGRES_PASSWORD`, `DATABASE_URL`, `DATABASE_ADMIN_URL` (+ `TELEMETRY_KEY/SALT` et les
6 tokens DM-2).

## ⚠️ À FAIRE par ops AVANT le prochain `apply -k` (sinon casse)

Pour **chaque** environnement déjà déployé (scaleway, dgx) :

1. Copier `secret-patch.yaml.example` → `secret-patch.yaml` (s'il n'existe pas déjà).
2. Y reporter **toutes** les clés secrètes ci-dessus avec leurs **valeurs actuelles**
   (les lire depuis le secret live :
   `kubectl -n bootstrap get secret device-management-secrets -o jsonpath='{.data.CLE}' | base64 -d`).
   → Pour `POSTGRES_PASSWORD`/`DATABASE_URL` : reprendre la valeur **existante**
   (ne pas rotationner sans migration du PVC postgres).
3. Vérifier le build : `kubectl kustomize deploy/k8s/overlays/<env>` (aucun `<CHANGEME>` résiduel).

## 🔑 Rotation (secrets exposés en historique git)

Ces valeurs étaient committées en clair → **les considérer comme compromises et rotationner** :

- `LLM_API_TOKEN` (token API Scaleway) → régénérer côté Scaleway, mettre à jour les overlays.
- `DM_TELEMETRY_UPSTREAM_KEY` → régénérer côté collecteur télémétrie.
- `DM_TELEMETRY_TOKEN_SIGNING_KEY` → régénérer (invalide les tokens télémétrie en cours).

(Le retrait du fichier ne purge pas l'historique ; la rotation est la seule mitigation réelle.)

## Nouvel environnement (prod-beta, et futurs)

1. `cp overlays/prod-beta/secret-patch.yaml.example overlays/prod-beta/secret-patch.yaml`,
   renseigner les `<CHANGEME>`.
2. Compléter `overlays/prod-beta/kustomization.yaml` (`<TODO_TAG>`, ingress/host spécifiques).
3. `kubectl kustomize overlays/prod-beta` puis apply.
