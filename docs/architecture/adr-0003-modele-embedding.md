# ADR-0003 — Choix du modèle d'embedding pour la recherche RAG du plugin

- **Statut** : Accepted
- **Date** : 2026-07-14 (décision appliquée depuis DM 0.9.1 / plugin 0.13.5, 2026-07-13)
- **Décideurs** : eric.tiquet + Claude Fable 5
- **Sujet** : quel modèle d'embedding sert la recherche vectorielle du plugin matisse,
  et par quel canal il est servi et désigné.

## Contexte

La fonctionnalité « Recherche et résumé » du plugin Thunderbird indexe la boîte de
réception en vecteurs d'embedding puis fait de la recherche sémantique (RAG). Le client
est figé (TB 60 / Gecko 60, cf. ADR-0002) : il ne peut ni embarquer de secret, ni gérer
un second canal d'authentification, ni être re-packagé au rythme des évolutions de
modèles. Le proxy `/llm/v1` (ADR-0002) masque déjà la clé backend et authentifie chaque
client par `llmToken` minté. Le backend LLM (Scaleway Generative APIs sur int) expose
chat ET embeddings sur le même contrat OpenAI. Contraintes : corpus francophone
(courriel administratif), postes clients modestes, aucune variable de configuration
superflue côté sites air-gap.

## Options considérées

### Option A — Modèle d'embedding du backend LLM existant (`bge-multilingual-gemma2`), servi par le proxy `/llm/v1`
- Le proxy ajoute une route `/embeddings` en passthrough ; le modèle est désigné par
  une unique variable serveur `EMBD_MODEL_NAME` ; le client réutilise
  `embdUrl = llmEndpoint` et le `llmToken` déjà mintés.
- Pros : zéro nouveau secret ni endpoint côté client et côté site ; même quota, même
  audit, même masquage de clé ; modèle multilingue performant en français ; changement
  de modèle = changement de variable.
- Cons : dépendance à la disponibilité du modèle chez le fournisseur ; un changement de
  modèle invalide les index vectoriels des postes (ré-indexation).

### Option B — Serveur d'embedding dédié auto-hébergé (TEI/vLLM, p. ex. sur le DGX)
- Pros : indépendance fournisseur, latence maîtrisable, choix de modèle libre.
- Cons : un composant de plus à exploiter et homologuer par site ; un secret et un
  endpoint de plus à diffuser aux clients figés ; contraire au principe de sécabilité
  minimale de l'ADR-0002 pour un besoin encore jeune.

### Option C — Embedding local au poste client
- Le moteur RAG du plugin possède un repli local (dev).
- Pros : zéro réseau, zéro dépendance serveur.
- Cons : postes bureautiques anciens (CPU), qualité nettement moindre, modèle embarqué
  dans l'XPI impossible à faire évoluer sans re-packager le parc.

## Décision

**Option retenue : A** — `bge-multilingual-gemma2` servi par le proxy `/llm/v1`
(route `/embeddings`), désigné par la seule variable `EMBD_MODEL_NAME`.

Le besoin est couvert sans étendre la surface du client ni celle des sites : le canal,
l'auth, le quota et l'audit existent déjà (ADR-0002). Côté client, la découverte est
dynamique : le plugin sonde les modèles annoncés par `/models` et écarte ceux qui
rejettent `/embeddings` (les modèles de chat répondent 422) — le bon embedder est
sélectionné sans logique codée en dur. On accepte en contrepartie la dépendance au
catalogue du fournisseur et le coût d'une ré-indexation si le modèle change.

## Conséquences

- **Positives** : un seul contrat OpenAI pour chat + embeddings ; bascule de modèle par
  configuration (hot-reload) ; aucun secret d'embedding sur les postes ; validé en
  conditions réelles sur int (indexation effective, 2026-07-14).
- **Négatives** : les index vectoriels des postes sont couplés au modèle — tout
  changement d'`EMBD_MODEL_NAME` impose une ré-indexation silencieuse du parc ; le
  probing client génère un bruit de 422 dans l'audit LLM (bénin, documenté).
- **À surveiller** : dépréciation de `bge-multilingual-gemma2` au catalogue du
  fournisseur ; volume d'appels `/embeddings` vs quotas ; si le bruit de probing gêne,
  faire sonder en priorité le `embdModel` diffusé par `/config`.

## Suivi

- [x] Implémentation : PR device-management#27 (embedder RAG), plumbing
  `EMBD_MODEL_NAME` (CI + manifests), plugin `rag-integration.js` (0.13.5+)
- [x] Validation int : embeddings 200 sur `bge-multilingual-gemma2` (logs llm-proxy,
  2026-07-14)
- [x] Doc opérateur : `docs/operations/llm-proxy.md` (section embeddings)
- [x] ADR-0002 §5 mis à jour (frontière validée par l'usage)
