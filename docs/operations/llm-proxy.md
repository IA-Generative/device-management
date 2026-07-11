# Proxy LLM `/llm/v1` — exploitation et configuration

> **Public : opérateur / administrateur DM.** Pour le *pourquoi* architectural (découplage,
> sécabilité, choix Postgres), voir [../architecture/adr-0002-proxy-llm-relais.md](../architecture/adr-0002-proxy-llm-relais.md).
> Pour le code, les docstrings des modules [`app/llm/`](../../app/llm/) font référence.

Depuis la **0.9.0**, le DM relaie le trafic LLM des plugins : passerelle de compatibilité
pour les clients figés (Thunderbird 60 : TLS 1.3 draft-23, pas de gestion de certificats)
et point d'application des règles (quotas, guardrails, routage de backend, audit). La clé
du backend réel (`LLM_API_TOKEN`) **ne quitte jamais le serveur**.

---

## Le trajet d'une requête (où chaque clé intervient)

### 1. Le plugin récupère sa config — `GET /config/{device}/config.json`

| Clé | Rôle |
|---|---|
| `FORCE_LLM_ENDPOINT_OVERRIDE` | **Interrupteur maître** (défaut `true`). `true` : la réponse annonce `llmEndpoint` = le proxy DM ; `false` : mode direct historique (debug). Bascule/rollback **à chaud** depuis l'onglet Config admin (propagation ~3 s, effectif au prochain poll `/config` des plugins). |
| `PUBLIC_LLM_PROXY_URL` | URL exacte écrite dans `llmEndpoint`. Défaut : dérivée de `PUBLIC_BASE_URL` + `/llm/v1`. À poser seulement si le proxy est exposé sous un autre host/chemin. |
| `DM_LLM_TOKEN_SIGNING_KEY` 🔐 | Fabrique le « badge » du client : le vrai token backend ne sortant plus, le champ `llmToken` reçoit un jeton par client `{client_uuid, email, exp}` **signé HMAC** avec cette clé — minté uniquement après validation de la paire `X-Relay-Client/Key`, jamais mis en cache. Auto-générée par `dumb-deploy.sh` (repo privé). |
| `DM_LLM_TOKEN_TTL_SECONDS` | Durée de vie du badge (défaut 3600 s). Le plugin en reçoit un frais à chaque poll `/config`. |

### 2. Le plugin appelle le LLM — `POST /llm/v1/chat/completions`, `GET /llm/v1/models`

**Auth duale** (le comportement du client figé étant incertain, les deux vecteurs sont acceptés ;
la source de vérité reste le relay client validé, révocation re-vérifiée en DB) :
en-têtes `X-Relay-Client`/`X-Relay-Key`, **ou** `Authorization: Bearer <llmToken>` — vérifié
avec la même `DM_LLM_TOKEN_SIGNING_KEY` (partagée par tous les pods → n'importe quel réplica
vérifie n'importe quel token : le proxy reste stateless).

Puis le **pipeline d'intercepteurs** (ordre fixe : throttle → guardrails) :

| Clé | Rôle |
|---|---|
| `LLM_QUOTA_REQUESTS_PER_MINUTE` | Quota **par utilisateur** (fenêtre fixe). **`0` = désactivé (défaut)** : le point d'accroche est livré armé mais neutre. Compteurs PostgreSQL (`llm_quota_counters`) **partagés entre réplicas**, créés lazy à la première requête — aucune initialisation à opérer. Dépassement → `429 {"error", "retry_after"}` + header `Retry-After`. |
| `LLM_QUOTA_WINDOW_SECONDS` | Taille de la fenêtre (défaut 60 s). Les requêtes refusées comptent aussi (sémantique fixed-window standard). |
| `LLM_GUARDRAILS` | Liste **ordonnée** (CSV) des règles actives, appliquées au prompt entrant ET à la réponse. Livrées : `noop` (défaut, pass-through) et `deny_all` (**coupe-circuit d'urgence** : tout le trafic LLM refusé en ~3 s, réversible de la même façon). |

### 3. Le proxy relaie vers le backend réel

| Clé | Rôle |
|---|---|
| `LLM_BASE_URL` / `LLM_API_TOKEN` 🔐 | Backend OpenAI-compatible par défaut + sa clé, injectée **côté serveur uniquement** (alias du ticket : `LLM_BACKEND_URL`/`LLM_BACKEND_API_KEY`). |
| `LLM_BACKENDS` | Registry multi-backends (JSON) : autres URLs + mapping modèle→backend (fnmatch). Les clés y sont référencées par **indirection** `token_env` (nom d'une variable d'env) — jamais de secret dans le JSON ni dans l'UI. Ajout/bascule/failover **sans code ni redéploiement**. |
| `DM_LLM_READ_TIMEOUT_SECONDS` | Timeout de lecture backend (défaut 120 s). En streaming il s'applique **entre deux chunks**, pas à la durée totale : une génération longue qui « parle » n'est jamais coupée, un backend muet si. |

Exemple `LLM_BACKENDS` :
```json
{
  "backends": {
    "mistral": {"base_url": "https://api.exemple/v1", "token_env": "LLM_API_TOKEN_MISTRAL"}
  },
  "model_map": {"mistral-*": "mistral"}
}
```

**Streaming** : `stream: true` est relayé chunk par chunk (SSE), sans bufferisation — la
backpressure du client se propage au backend, mémoire constante par stream. Limite assumée :
en sortie streamée, l'inspection guardrail est best-effort par chunk (une analyse sémantique
complète imposerait de bufferiser).

---

## Opérations courantes (onglet Config admin — tout est hot-reload)

| Besoin | Action | Effet |
|---|---|---|
| **Basculer la flotte sur le proxy** | `FORCE_LLM_ENDPOINT_OVERRIDE = true` (défaut) | Au prochain poll `/config` de chaque plugin |
| **Rollback d'urgence en mode direct** | `FORCE_LLM_ENDPOINT_OVERRIDE = false` | Idem — aucun redéploiement |
| **Couper tout le trafic LLM** (incident) | `LLM_GUARDRAILS = deny_all` | ~3 s, toute la flotte ; retour : `noop` |
| **Activer un quota** | `LLM_QUOTA_REQUESTS_PER_MINUTE = 30` | Immédiat, compteurs cohérents multi-réplicas |
| **Ajouter / basculer un backend** | Éditer `LLM_BACKENDS` (+ poser la var `token_env` au déploiement) | Par requête suivante |
| **Faire tourner la clé de signature** | Nouvelle valeur `DM_LLM_TOKEN_SIGNING_KEY` | ⚠️ invalide les llmToken en circulation (les plugins en re-reçoivent au poll suivant ; l'auth X-Relay n'est pas affectée) |

**Brancher une vraie règle guardrail** (développeur) : une classe héritant de
`Guardrail` (`check(payload, direction, ctx) → allow|deny|transform`), enregistrée dans
`GUARDRAIL_REGISTRY` ([app/llm/guardrails.py](../../app/llm/guardrails.py)), nommée dans
`LLM_GUARDRAILS`. Zéro modification du cœur — c'est testé (`tests/test_llm_pipeline.py`).

---

## Modèle de déploiement

Le proxy est **stateless** : tout état partagé (quotas, révocation relay, config runtime)
vit dans PostgreSQL → N réplicas derrière le LB **sans affinité de session**.

- **k8s** : deployment dédié `llm-proxy` (même image, `DM_RUNTIME_MODE=llm` : seules les
  routes `/llm/v1` + sondes + `/metrics` servent, **sans PVC** → scale réel même sur les
  overlays où le PVC RWO force l'API à 1 réplica). HPA 2→10. Route d'entrée : path `/llm`
  de l'Ingress (annotations SSE **requises** : `proxy-buffering: "off"`,
  `proxy-read-timeout: "300"`) ; DGX : HTTPRoute `/bootstrap/llm` → rewrite `/llm` ;
  prod-sdid : Route OpenShift `bootstrap-llm`.
- **docker-compose** : service `llm-proxy` (port 8090 par défaut).
- Le pod API a aussi besoin du mapping `DM_LLM_TOKEN_SIGNING_KEY` (c'est lui qui mint dans
  `/config`) — présent dans `20-device-management-deployment.yaml`.

## Observabilité

- **`/metrics`** (Prometheus) : `dm_llm_request_duration_seconds` (histogramme → p50/p95/p99
  via `histogram_quantile`), `dm_llm_requests_total{route,model,backend,status}`,
  `dm_llm_errors_total{kind}`, `dm_llm_active_requests`, `dm_llm_quota_denied_total`.
- **Audit** : une ligne JSON par requête sur stdout (logger `dm-llm-audit`) — trace_id
  (`X-Request-Id`, propagé client→proxy→backend), identité, modèle, backend, verdicts,
  quota, statut, latence, usage tokens. Jamais de secret ni de contenu de prompt.

## Dépannage

| Symptôme | Cause probable | Remède |
|---|---|---|
| Streams coupés à ~60 s | Annotations SSE absentes sur l'ingress | `proxy-read-timeout: "300"` (+ `proxy-buffering: "off"`) |
| Réponse SSE arrive d'un bloc | Bufferisation d'un proxy intermédiaire | Vérifier `proxy-buffering: off` ; le proxy émet déjà `X-Accel-Buffering: no` |
| `503` sur `/llm/v1` au démarrage | Readiness gate : config runtime pas encore chargée (DB injoignable ?) | Normal quelques secondes ; sinon vérifier `DATABASE_URL` et les logs `runtime_config` |
| `401` avec un llmToken valide | Clé de signature absente/différente entre pods, ou client révoqué | Vérifier `DM_LLM_TOKEN_SIGNING_KEY` dans le secret + mapping sur **API et llm-proxy** |
| `llmToken` vide dans `/config` | Clé de signature non posée, ou requête sans `X-Relay-*` valides | Poser la clé ; le token n'est minté que pour un relay client validé |
| `502 backend_unreachable` sur DGX | Sortie corporate non configurée | Patch `proxy-patch-llm-proxy.yaml` (HTTP_PROXY/NO_PROXY) — pas de WireGuard sur DGX |
| `508 backend_loop` | `LLM_BASE_URL` pointe le proxy lui-même | Corriger l'URL backend (garde anti-boucle volontaire) |
| Quota jamais déclenché | Valeur ≤ 0 (défaut) ou store mémoire (pas de DB) | Poser `LLM_QUOTA_REQUESTS_PER_MINUTE` > 0 ; vérifier la DB |

## Vérification rapide (smoke)

```bash
# 1. Le /config annonce le proxy (défaut ON)
curl -s $BASE/config/config.json -H "X-Plugin-Version: 1.0" | jq .config.llmEndpoint
# 2. Auth exigée (401 OpenAI propre)
curl -s $BASE/llm/v1/models
# 3. Avec credentials relay (enrôlement préalable) : 200 + modèles du backend
curl -s $BASE/llm/v1/models -H "X-Relay-Client: $RC" -H "X-Relay-Key: $RK"
# 4. Stream progressif (le -N désactive le buffering curl)
curl -N -s -X POST $BASE/llm/v1/chat/completions -H "X-Relay-Client: $RC" -H "X-Relay-Key: $RK" \
  -H "Content-Type: application/json" -d '{"stream":true,"messages":[{"role":"user","content":"salut"}]}'
```

Suites automatisées : `tests/test_llm_*.py` (unitaires) ; `tests/test_llm_quota_pg.py`
(marqueur `integration`, vrai Postgres — exactitude des compteurs multi-réplicas sous
concurrence).
