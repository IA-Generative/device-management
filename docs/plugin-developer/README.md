# Développeur de plugin — intégration

> **Public : développeur/intégrateur de plugin** (et consommateur de l'API catalogue).
> Comment un plugin s'enrôle, récupère sa config, se met à jour, et comment publier au catalogue.
> Pour le *pourquoi* architectural, voir [../architecture/](../architecture/).

| Document | Usage |
|---|---|
| [consumer-readme.md](consumer-readme.md) | **Démarrer ici** — intégration client : plugins supportés, PKCE, endpoints, exemples cURL |
| [plugin-integration-2-4-5.md](plugin-integration-2-4-5.md) | Parcours détaillé : fetch config minimale, télémétrie pré-login, SSO Keycloak PKCE |
| [plugin-dm-protocol-update-features.md](plugin-dm-protocol-update-features.md) | Protocole Plugin ↔ DM : déploiement progressif, feature toggling, `/update/status` |
| [packaging-guide.md](packaging-guide.md) | Préparer un plugin pour détection/enregistrement automatique dans le catalogue |
| [config.default.example.json](config.default.example.json) | Exemple de configuration par défaut d'un plugin |
| [mirai-integration-README.md](mirai-integration-README.md) | Intégrer le catalogue DM dans un portail tiers (snippet Wagtail/DSFR) |
| [mirai-catalog-snippet.html](mirai-catalog-snippet.html) | Snippet HTML prêt à coller (API `/catalog/api/plugins`) |

Vue d'ensemble du modèle de distribution de valeur : [../architecture/adr-0001-vue-densemble.md §3](../architecture/adr-0001-vue-densemble.md).
