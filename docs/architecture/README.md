# Architecture — décisions (ADR)

> **Public : architecte.** Pourquoi le système est conçu ainsi. Pour *utiliser* DM côté plugin,
> voir [../plugin-developer/](../plugin-developer/) ; pour le périmètre audit, [../security/](../security/).

Les ADR (Architecture Decision Records) consignent les décisions structurantes et leur justification.

| ADR | Sujet | Statut |
|---|---|---|
| [adr-0001-vue-densemble.md](adr-0001-vue-densemble.md) | **Point d'entrée** — fonctionnement, choix d'architecture, modèle de distribution de valeur au plugin, modèle de sécurité | En vigueur |
| [adr-product-architecture.md](adr-product-architecture.md) | Décisions détaillées §2.1–2.10 (monolithe, PostgreSQL, pipeline config, relay, admin UI, auth, Kustomize, catalogue, binaires, télémétrie) + dette technique + roadmap | En vigueur |
| [adr-0002-proxy-llm-relais.md](adr-0002-proxy-llm-relais.md) | Proxy LLM `/llm/v1` (0.9.0) + **principe de sécabilité** : séparation des préoccupations par composant, hypothèse de long terme (le porteur de chaque composant évoluera), périmètres d'homologation alignés sur les frontières, règles de découplage opposables | En vigueur |
| [adr-dgx-deployment.md](adr-dgx-deployment.md) | Stratégie de déploiement on-premise DGX (proxy, WAF, registry mirror, secrets) | Accepté |

**Par où commencer** : lire [adr-0001](adr-0001-vue-densemble.md) pour la vue d'ensemble, puis
plonger dans [adr-product-architecture](adr-product-architecture.md) pour le détail d'une décision.
