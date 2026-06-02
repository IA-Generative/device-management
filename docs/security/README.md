# Sécurité — périmètre auditeur

> **Public : auditeur de sécurité.** Constats, corrections livrées, conformité référentielle,
> points de vigilance et doctrine. Le *modèle de sécurité* (conception) est décrit dans
> [../architecture/adr-0001-vue-densemble.md §4](../architecture/adr-0001-vue-densemble.md).

| Document | Contenu |
|---|---|
| [audit-remediation-report.md](audit-remediation-report.md) | Rapport de remédiation : vulnérabilités corrigées (IMM/CT), conformité référentielle, points de vigilance opérationnels, doctrine de sécurité retenue |

## Modèle de sécurité — repères rapides
- **Auth multi-surface** (config public / enroll PKCE / relay hashé / télémétrie JWT / admin OIDC+PKCE) — [adr-0001 §4.1](../architecture/adr-0001-vue-densemble.md).
- **Boot gate fail-closed** : refus de démarrage en prod si secrets au défaut, `DM_ALLOW_ORIGINS=*`, ou `DM_DEV_AUTOLOGIN` actif — [adr-0001 §4.2](../architecture/adr-0001-vue-densemble.md).
- **Callback OIDC admin** : ID Token vérifié comme JWS (PyJWKClient), redirect dérivé de `PUBLIC_BASE_URL`, cookie CSRF — [adr-0001 §4.3](../architecture/adr-0001-vue-densemble.md).
- **Doctrine secrets** : secrets jamais exposés (relay), masquage consolidé, hors-repo, rotation — [adr-0001 §4.4](../architecture/adr-0001-vue-densemble.md).
