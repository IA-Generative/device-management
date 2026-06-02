# Rapport de remédiation — Audit de sécurité (backend device-management)

> Référence : *Audit de code — Backend device-management* (audit de sécurité externe), v1.0.
> Périmètre du présent rapport : corrections de sécurité immédiates (IMM) et court terme (CT)
> appliquées sur la base v0.6.0.

---

## 0. Résumé exécutif

Lot de remédiation **livré et vérifié** :
- **IMM-1 → IMM-8** (8 corrections : 7 Élevées/Critiques + une Faible).
- **CT-1** révocation relay, **CT-7** verrou cache OIDC, **CT-9** outillage CI, **CT-12** bornes
  hautes des dépendances.
- **Boot gate validé end-to-end en conteneur** : en dev → import OK ; en prod avec secrets par
  défaut → refus de démarrage avec motifs explicites ; en prod avec secrets corrects + origines
  explicites → boot OK (vérification fail-closed la plus forte possible hors cluster).

Vulnérabilités **corrigées dans ce lot** : VULN-001, 002, 003, 005, 007, 012, 013, 014, 016, 017.

> **Comportement fail-closed confirmé** : en environnement de production, l'application **refuse de
> démarrer** si un secret est resté à sa valeur par défaut, si la configuration CORS est trop
> permissive (`*`/vide) ou si le mode auto-login de développement est actif. Ce garde-fou a permis
> de détecter une vraie misconfiguration d'injection de secret lors d'un déploiement (le gate a
> attrapé la précondition manquante au lieu de démarrer dans un état non sûr).

---

## 1. Tableau des constats corrigés

Légende : ✅ Corrigé (ce lot) · 🟡 Déjà conforme / partiel · 📋 Doc/ADR.

### Vulnérabilités corrigées

| ID | Titre (sévérité) | Statut | Traitement |
|----|------------------|--------|-----------|
| **VULN-001** | Mode dev auto-login (Critique) | ✅ Corrigé | IMM-2 : auto-login derrière un flag explicite (off par défaut) + IMM-1 : boot refusé en prod si le flag est actif. `app/admin/auth.py`, `app/main.py:validate_security_config`. |
| **VULN-013** | Secrets par défaut compilés (Élevée) | ✅ Corrigé | IMM-1 : boot refusé en prod/staging si les secrets de session/relay sont aux valeurs par défaut. |
| **VULN-002** | Path traversal `/api/artifacts` (Élevée) | ✅ Corrigé | IMM-4 : validation regex device_type/version/filename + `os.path.basename` + `_safe_path_join`. `app/main.py`. |
| **VULN-005** | Comparaison non constant-time (Élevée) | ✅ Corrigé | IMM-3 : `hmac.compare_digest` dans `_verify_admin_token`. `app/main.py`. |
| **VULN-007** | `/update/status` non authentifié (Élevée) | ✅ Corrigé | IMM-5 : exige les credentials relay + binding `client_uuid`. `app/main.py:report_update_status`. |
| **VULN-014** | CORS `*` par défaut (Faible) | ✅ Corrigé | IMM-1/6 : `*` refusé en prod, plus de fallback wildcard. |
| **VULN-016** | Masquage secrets incomplet (Faible) | ✅ Corrigé | IMM-7 : `SENSITIVE_ENV_VARS` + `is_sensitive_key()` ; masquage longueur + SHA-256. `app/services/crypto.py`, `app/admin/router.py`. |
| **VULN-017** | ID Token OIDC admin non vérifié (Élevée) | ✅ Corrigé | IMM-8 : vérification JWS via `PyJWKClient` (signature + iss/aud/exp). `app/admin/router.py:/callback`. |
| **VULN-003** | Révocation relay absente (Élevée) | ✅ Corrigé | CT-1 : `POST /admin/devices/{client_uuid}/revoke-relay` pose `revoked_at=now()` (audité). |
| **VULN-012** | Cache OIDC non thread-safe (Moyenne) | ✅ Corrigé | CT-7 : `threading.Lock` + double-checked locking dans `auth.py:_get_oidc_config`. |
| **VULN-008-bis** | `alg=none` résiduel (Moyenne) | 🟡 Déjà conforme | `algorithms` explicite, défaut RS256 (`app/main.py`, `settings.py`). |
| **VULN-015** | Hash relay sans sel/compte (Faible) | 📋 Acceptable, à documenter | Conforme RGS-B1 (clé 256 bits `os.urandom`). ADR à produire. |

### Conformité référentielle (synthèse)

| Référentiel | Constat audit | Statut après ce lot |
|-------------|---------------|---------------------|
| RGS-B1 (crypto) | Conforme | ✅ Préservé |
| RGS-B2 (gestion clés) | Non conforme (défauts, rotation) | 🟡 Boot gate (IMM-1) ferme les défauts en prod |
| RGS-B3 (auth) | Partielle | 🟡 constant-time (IMM-3) + ID Token (IMM-8) + révocation (CT-1) faits |
| PA-080 R26/R28 (ID Token) | Non conforme | ✅ IMM-8 |
| MFA / step-up | — | Délégué au fournisseur d'identité (pas de code backend) |

---

## 2. Détail des corrections livrées

Fichiers : `app/main.py`, `app/admin/auth.py`, `app/admin/router.py`, `app/services/crypto.py`.

- **IMM-1/6** `validate_security_config()` (`app/main.py`) : appelée au chargement ; en
  prod/staging lève `RuntimeError` (refus de boot) si les secrets de session/relay sont aux défauts,
  si CORS est `*`/vide, ou si l'auto-login de dev est actif. En dev : warnings seulement.
- **IMM-2** (`app/admin/auth.py`) : flag explicite (off par défaut) requis pour l'auto-login dev.
- **IMM-3** (`app/main.py`) : `hmac.compare_digest`.
- **IMM-4** (`app/main.py` `/api/artifacts`) : validation stricte + `_safe_path_join`.
- **IMM-5** (`app/main.py` `/update/status`) : auth relay + binding `client_uuid`.
- **IMM-7** (`app/services/crypto.py`, `app/admin/router.py`) : `SENSITIVE_ENV_VARS` +
  `is_sensitive_key()` + `mask_secret()` non révélateur (longueur + SHA-256 tronqué).
- **IMM-8** (`app/admin/router.py` `/callback`) : vérification JWS de l'ID Token via `PyJWKClient`.

---

## 3. Points de vigilance opérationnels

1. **Fail-closed** : en prod, l'app refuse de démarrer si les secrets de session/relay sont aux
   défauts ou si la configuration CORS est trop permissive. S'assurer que **tous** les secrets requis
   et la liste explicite des origines autorisées (`DM_ALLOW_ORIGINS`) sont fournis par l'overlay de
   déploiement avant le rollout.
2. **IMM-8 modifie le flux de login admin** : à valider contre le fournisseur OIDC réel (audience =
   client admin, issuer = discovery). Garder une procédure de rollback prête.
3. **Rotation des secrets** : tout secret applicatif (token LLM, clés de télémétrie, secret OIDC
   admin) doit être fourni exclusivement via les secrets de déploiement (jamais committé) et faire
   l'objet d'une rotation régulière.

---

## 4. Doctrine de sécurité retenue

- **Secrets** : aucune valeur réelle dans le dépôt ; injection via secrets de déploiement (overlays
  gitignorés) ; boot gate fail-closed qui refuse les valeurs par défaut en production.
- **CI sécurité** : outillage `ruff`/`bandit`/`pip-audit`/`semgrep` (`.github/workflows/ci.yml`),
  bornes hautes des dépendances.
- **MFA / Token Exchange** : délégués au fournisseur d'identité, hors périmètre backend.
