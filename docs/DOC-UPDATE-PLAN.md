# Plan de mise à jour de la documentation (docs/)

> Contexte : la remédiation sécurité (branche `fix/security-imm-quickwins`, mergée sur main) introduit
> des comportements que la doc doit refléter. Ce plan liste, par fichier, ce qui est obsolète et l'action.
> Source de vérité détaillée : [adr/audit-remediation-report.md](adr/audit-remediation-report.md).

## Changements à documenter

1. **Boot gate fail-closed** (`app/main.py:validate_security_config`) : refus de démarrage en
   prod/staging/production si `ADMIN_SESSION_SECRET` ou `DM_RELAY_SECRET_PEPPER` au défaut, si
   `DM_ALLOW_ORIGINS` vaut `*`/vide, ou si `DM_DEV_AUTOLOGIN` est actif.
2. **`DM_DEV_AUTOLOGIN`** (nouveau) : auto-login admin dev, opt-in, OFF par défaut.
3. **`POST /admin/devices/{client_uuid}/revoke-relay`** (nouveau) : révocation des credentials relay.
4. **`/update/status`** exige désormais des credentials relay (401 sinon).
5. **Callback OIDC admin** : ID Token vérifié comme un JWS (PyJWKClient).
6. **Masquage secrets** consolidé : `SENSITIVE_ENV_VARS` + `is_sensitive_key()`/`mask_secret()`.
7. **`GET /catalog/api/plugins`** expose le champ `id` (slug→id).
8. **Outillage CI** : `.github/workflows/ci.yml`, `pyproject.toml` (ruff/bandit), `requirements-dev.txt`.

## Actions par fichier

### 🔴 Critique — bloque la compréhension du déploiement
- **`../deploy/DEPLOY-RUNBOOK.md`** : ajouter une section « Boot Gate » listant les 4 vars requises en
  prod (avec exemple `env-secrets.yaml`) + un encart troubleshooting « pod CrashLoopBackOff →
  `Refusing to start` ». Distinguer « requis (sinon crash) » de « rotation recommandée ».
- **`guides/developer-readme.md`** (section « Variables d'environnement ») : ajouter le tableau boot-gate
  (var · requis · comportement dev · comportement prod) avant « Essentielles ».
- **`dgx/RUNBOOK-DGX.md`** : après le bloc `~/.dm-secrets/`, expliquer les secrets requis par le gate +
  note dans « Changer un secret » (restart pod pour re-valider).

### 🟠 Élevé — complétude API
- **`adr/consumer-readme.md`** : ajouter l'endpoint `revoke-relay` ; documenter que `/update/status`
  exige les en-têtes relay (`X-Relay-Client`/`X-Relay-Key`, 401 sinon) ; 2 entrées troubleshooting.
- **`adr/packaging-guide.md`** : documenter le champ `id` désormais présent dans `GET /catalog/api/plugins`
  (résolution slug→id pour `POST /admin/catalog/{id}/versions/upload`).

### 🟡 Moyen — architecture & sécurité
- **`adr/adr-product-architecture.md`** : §2.6 marquer le défaut `ADMIN_SESSION_SECRET` comme **corrigé
  (IMM-1)** ; §2.4 ajouter la révocation relay ; nouvelle §2.10 « Security Boot Gate » (décision,
  checks, justification fail-closed, comportement dev vs prod).
- **`prompts/prompt-plugin-dm-integration.md`** : vérifier/ajouter l'exigence d'auth relay sur `/update/status`.
- **`prompts/prompt-admin-ui.md`** : statut du bouton UI optionnel de révocation relay.
- **`guides/mirai-integration-README.md`** : mentionner le champ `id` du catalogue.

### 🟢 Nouveaux docs recommandés (optionnels)
- **`adr/str-secrets-doctrine.md`** (ADR STR-7) : doctrine de gestion/masquage des secrets.
- **`guides/security-posture.md`** : page opérateur (gates, checklist pré-déploiement, pannes courantes).
- **`guides/relay-credential-lifecycle.md`** : cycle enroll → TTL 30 j → revoke → ré-enrôlement.

## Note d'organisation
Les fichiers `audit-remediation-report.md`, `consumer-readme.md`, `packaging-guide.md`,
`plugin-integration-2-4-5.md`, `plugin-dm-protocol-update-features.md`, `config.default.example.json`
ont été regroupés sous `docs/adr/` (déplacement pur, contenu identique) pour cohérence.
