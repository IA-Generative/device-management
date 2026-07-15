"""Proxy LLM OpenAI-compatible du Device Manager (/llm/v1).

Passerelle de compatibilité pour les clients figés (plugin Thunderbird matisse,
TLS 1.3 draft-23) et point d'application des règles. Le trafic LLM du plugin est
routé ici (via l'override llmEndpoint de /config), puis relayé vers le backend
réel dont l'URL et la clé ne quittent JAMAIS le serveur.

Architecture — pipeline d'intercepteurs enfichables autour d'un cœur minimal :

    auth duale (X-Relay-Client/Key OU llmToken signé)  →  identité utilisateur
    PRÉ-REQUÊTE : throttle (quota partagé Postgres) → guardrails (entrée)
    forward httpx (pool partagé, passthrough SSE sans bufferisation)
    POST-RÉPONSE : guardrails (sortie) → audit structuré + métriques Prometheus

Points d'accroche (chacun extensible par CONFIGURATION, sans toucher au cœur) :
- ``guardrails.py``  : Guardrail.check(payload, direction, ctx) → allow|deny|transform ;
  sélection ordonnée via la clé LLM_GUARDRAILS (hot-reload).
- ``throttle.py``    : QuotaStore (abstraction ; impl Postgres fenêtre fixe) ;
  limites via LLM_QUOTA_* (hot-reload, 0 = désactivé).
- ``backends.py``    : BackendRegistry piloté par LLM_BASE_URL/LLM_API_TOKEN
  (+ LLM_BACKENDS JSON pour multi-backends / mapping modèle→backend).
- ``audit.py``       : une ligne JSON par requête (trace-id propagé), sans secret
  ni contenu de prompt.

Modèle d'exécution : STATELESS — tout état partagé (quotas, révocation, config)
vit dans PostgreSQL → N réplicas derrière un LB sans affinité de session.
Montage : ``build_router()`` (cf. router.py), inclus par app/main.py pour les
runtime modes api | all | llm (mode ``llm`` = deployment dédié scalable).
"""
