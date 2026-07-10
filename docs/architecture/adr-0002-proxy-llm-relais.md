# ADR-0002 : Proxy LLM dans le DM — découplage des domaines de responsabilité et sécabilité

**Date** : 2026-07-10
**Statut** : En vigueur
**Auteurs** : eric.tiquet + Claude Fable 5
**Portée** : introduction du relais LLM OpenAI-compatible `/llm/v1` (package `app/llm/`,
deployment `llm-proxy`) et de l'override `FORCE_LLM_ENDPOINT_OVERRIDE` du `llmEndpoint`
annoncé aux plugins via `/config`.

---

## Contexte

Le plugin Thunderbird « matisse » (TB 60.9.1 / Gecko 60) est **figé** : il ne parle que
TLS 1.3 draft-23 — que les serveurs modernes rejettent — et n'a aucune gestion de
certificat auto-signé. On ne peut pas le corriger côté client. Les backends LLM, eux,
évoluent en permanence (terminaison TLS, URL, clés, modèles). Sans intermédiaire, chaque
évolution du backend casse la flotte.

La décision : router tout le trafic LLM du plugin à travers le DM, qui devient la
**passerelle de compatibilité** (le plugin ne parle qu'à la terminaison TLS du DM, déjà
compatible puisqu'il atteint `/config` et `/enroll`) et le **point d'application des
règles** (quotas, guardrails, routage de backend, audit).

## Décision

### 1. Frontière n°1 — IAssistant + DM d'un côté, backend LLM de l'autre

Le couple plugin IAssistant + DM porte **tout ce qui est spécifique au parc** : identité
et enrôlement (relay clients), compatibilité TLS du client figé, politiques (quotas,
guardrails, routage), configuration poussée par le canal `/config`, observabilité,
périmètre d'homologation. Le backend LLM n'est qu'un **fournisseur d'inférence
OpenAI-compatible, banalisé et interchangeable** : aucune logique métier, aucune
connaissance des clients, aucun couplage. Le seul contrat entre les deux mondes est
l'API OpenAI (`/chat/completions`, `/models`) plus le backend registry côté DM.

**Principe directeur — asymétrie des cycles de vie : le DM va évoluer, le backend LLM
non.** Toute règle nouvelle (anti-prompt-injection, PII, A/B, failover, quotas fins)
naît côté DM ; on change/ajoute/bascule de backend par configuration (`LLM_BACKENDS`,
hot-reload) sans redéploiement ni impact plugin. Corollaire : tout couplage qui
apparaîtra devra être placé côté DM, jamais côté backend.

### 2. Frontière n°2 — la fonction relais est SÉCABLE à l'intérieur du DM

La fonction est livrée comme un module autonome (`app/llm/`, aucune dépendance inverse
du cœur DM vers lui ; l'auth relay y est injectée à l'init), un runtime mode dédié
(`DM_RUNTIME_MODE=llm` : seules les routes `/llm/v1` + sondes + métriques servent) et un
Deployment dédié (`llm-proxy`, sans PVC, HPA indépendante). Elle est donc **extractible
en service séparé — voire en repo séparé — sans toucher ni au contrat plugin ni au
backend**, le jour où les contraintes d'homologation (périmètre DAT/AIPD distinct,
exigences de cloisonnement) ou le cycle de vie des besoins l'exigent. Le pipeline
d'intercepteurs (pré-requête / post-réponse) garantit que l'évolution des règles ne
modifie jamais le cœur du relais.

### 3. Décisions techniques associées

| Décision | Choix | Justification |
|---|---|---|
| Store des quotas | **PostgreSQL** (abstraction `QuotaStore`) | Conforme ADR-0001 (« pas de Redis, pas de broker ») ; UPSERT atomique = compteurs exacts entre N réplicas ; Redis branchable plus tard derrière l'interface si le débit l'exige |
| Auth entrante | **Duale** : X-Relay-Client/Key (vérif DB) OU `llmToken` signé HMAC par client, minté au `/config` (pattern telemetryKey), re-check de révocation | Le comportement exact du client figé est incertain ; la source de vérité reste le relay client validé ; la clé backend ne transite jamais |
| Override `/config` | `FORCE_LLM_ENDPOINT_OVERRIDE`, **défaut ON**, hot-reload (onglet Config admin, propagation ~3 s) ; appliqué APRÈS les overrides catalogue | Bascule de toute la flotte au prochain poll `/config`, sans ré-enrôlement ; rollback instantané (OFF = mode direct) |
| Métriques | `prometheus_client`, registry dédié concaténé au `/metrics` existant | Percentiles de latence (histogrammes) infaisables proprement à la main |
| Format d'erreur | Objet OpenAI `{"error": {...}}` + `retry_after` top-level (429) + header `Retry-After` | Exploitable à la fois par le plugin figé et par tout client OpenAI standard |
| Streaming | Passthrough SSE `aiter_raw` → `StreamingResponse`, zéro bufferisation, `read` timeout inter-chunk | Backpressure naturelle, mémoire constante par stream ; limite assumée : guardrail de sortie best-effort par chunk |

## Conséquences

- **Positives** : backend swappable à chaud ; périmètre d'homologation découpable ;
  montée en charge du relais indépendante du reste du DM (stateless, HPA 2→10, y compris
  sur les overlays où le PVC RWO force l'API à 1 réplica) ; toutes les règles futures
  s'ajoutent par configuration.
- **Coûts assumés** : un hop réseau et 1-2 allers-retours Postgres par requête
  (~1-5 ms, négligeable devant les secondes d'inférence LLM) ; une dépendance DB pour le
  quota (fail-open documenté) ; le nginx `relay-assistant` historique reste en place pour
  ses autres cibles — le relais LLM, retiré de nginx car sans valeur là-bas, renaît ici
  avec la valeur qui manquait (masquage de clé, quotas, audit).
