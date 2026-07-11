# Opérations / exploitation

> **Public : opérateur, responsable de déploiement, administrateur DM.** Faire tourner et exploiter
> le service. Pour intégrer un plugin, voir [../plugin-developer/](../plugin-developer/) ; pour le
> *pourquoi* architectural, [../architecture/](../architecture/).

| Document | Usage |
|---|---|
| [developer-readme.md](developer-readme.md) | Guide d'exploitation : dev local, docker-compose, services, ports, Kubernetes |
| [mode-operatoire-campagnes.md](mode-operatoire-campagnes.md) | Mode opératoire des campagnes de déploiement & feature toggling (administrateurs) |
| [llm-proxy.md](llm-proxy.md) | Proxy LLM `/llm/v1` (0.9.0) : rôle des clés, opérations courantes (quota, kill-switch, rollback, backends), déploiement, dépannage |
| [test-cahier.md](test-cahier.md) | Cahier de tests (unitaire, intégration, E2E, manuel) |
| [debug-campaign-not-serving.md](debug-campaign-not-serving.md) | Troubleshooting : campagne qui ne sert plus de directive update |
