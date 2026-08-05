# Journal d'hygiène — device-management

Mesures de dette technique, une entrée par passe, la plus récente en haut.
Règle de comparabilité : **mêmes outils, mêmes versions, mêmes flags** d'une passe
à l'autre. Tout changement d'outillage casse la courbe et doit être signalé.

Convention de périmètre : les métriques de code portent sur `app/` (le code
applicatif), le volume sur l'ensemble des fichiers suivis par git.

---

### Passe du 2026-07-26 (4/4) — lisibilité de la cohabitation — branche `fix/campaign-plugin-filter`

Déclenchée par une question d'usage, pas par une mesure : « comment voit-on que plusieurs
versions cohabitent ? ». La réponse était **mal** — les écrans livrés en 0.9.14 étaient
presque tous des champs de saisie. L'inspection a mis au jour deux défauts que ni la
passe 1 ni la passe 3 n'avaient vus, parce que **les deux n'avaient lu que des diffs,
jamais un écran**.

| Métrique | Passe 3/4 | Passe 4/4 | Delta |
|---|---|---|---|
| Statuts de version affichables | 4 sur 5 (`experimental` invisible) | **5 + repli** | +1 et un `else` |
| Endpoints publics d'accord sur « dernière version » | non (`MAX(version)` vs `published_at`) | **oui** | ✔ |
| Fonctions de test | 300 | **303** | +3 |
| Tests d'intégration exécutés en CI | 9 | **12** | +3 |
| Vérifications E2E rejouables | 26 | **30** | +4 |
| Surfaces publiques gardées contre une fuite d'`experimental` | 0 | **4** | +4 |

Défauts corrigés :

1. **Cellule Statut vide** — la cascade de badges de l'onglet Versions couvrait
   `published`/`draft`/`deprecated`/`yanked` sans `experimental` **ni `else`**. La 0.9.14
   avait élargi le `CHECK` en base et oublié l'affichage. Le `else` de repli garantit
   qu'un futur statut restera visible.
2. **« Dernière version » lexicographique** — `/catalog/api/plugins` et la grille
   résolvaient par `MAX(pv.version)` quand la fiche ordonnait par `published_at`. Prouvé
   sur Postgres réel : avec `0.13.9` et `0.13.22` toutes deux publiées, l'ancienne requête
   renvoyait `0.13.9`. Masqué tant qu'un seul rang est publié, mais la promotion d'une
   expé en main (mode opératoire §9.1) crée exactement cet état.

Ajouts : tag et badge dans l'admin, badge « Expe » + colonne Priorité sur les campagnes,
compteur « N versions en circulation » (affiché seulement si N > 1, calculé par agrégat
car `list_installations` est paginée), version stable et lien de retour sur la page
taguée, et `?exp=<tag>` sur l'API JSON.

**Garde d'étanchéité** — trois tests sur PostgreSQL réel vérifient qu'aucune surface
publique n'expose une version `experimental` sans son tag. Vérifié par mutation :
élargir un filtre « latest » fait rougir les tests.

Tendance : ↓ — deux bugs sortis, quatre surfaces publiques désormais gardées, et la
capacité livrée en 0.9.14 devient enfin lisible par un opérateur.

**Leçon de méthode, à retenir pour les prochaines passes** : relire un diff ne suffit pas
quand le diff élargit un domaine de valeurs. Il faut ouvrir l'écran — ou, à défaut, rendre
le gabarit avec les vraies formes de données, ce qui coûte quelques lignes et aurait
attrapé la cellule vide dès la première passe.

---

### Passe du 2026-07-26 (3/4) — revue du code produit par l'agent — branche `fix/qc-round2`

Deuxième application de la grille, cette fois **sur le code produit par l'agent lors de
la passe précédente** (règle 3.2 : une porte unique vaut aussi pour soi). Verdict
CORRIGER — deux bloquants, trois points majeurs.

| Métrique | Outil (version) | Passe 2/4 | Passe 3/4 | Delta |
|---|---|---|---|---|
| Copies de la règle d'auto-complétion | grep | 1 | 1 | = |
| Copies du repli `/binaries/` | grep | 2 (divergentes) | **1** | −1 |
| Complexité moyenne | radon 6.0.1 | B (5,26) | B (5,26) | = |
| Blocs CC > 15 | radon 6.0.1 | 30 | 30 | = |
| Fonctions de test | grep | 298 | **300** | +2 |
| Tests d'intégration **exécutés en CI** | ci.yml | **0** | **9** | +9 |
| Vérifications E2E rejouables depuis le dépôt | script | 0 (hors dépôt) | **26** | +26 |
| Lint / SAST | ruff 0.15.5 / bandit 1.9.2 | ruff 0 / bandit **exit 1** | ruff 0 / bandit **exit 0** | ✔ |
| Dépendances de build archivées | gh api | 1 (Kaniko) | **0** | −1 |

Défauts relevés et traités :

1. **Le repli `/binaries/` ne pouvait pas résoudre** — `artifacts.s3_path` est absolu en
   mode local, `get_binary` re-préfixe : l'URL produisait un chemin doublé, donc un 404.
   Le défaut préexistait et avait été recopié dans le code neuf. Une implémentation
   unique (`binaries_svc.route_rel_path`), 3 tests, mutation vérifiée.
2. **Kaniko archivé depuis 2025** — remplacé par BuildKit rootless, cf.
   [adr-0005](docs/architecture/adr-0005-build-in-cluster.md).
3. **Les tests d'intégration ne tournaient jamais** — la CI exécutait
   `pytest -m "not integration"`. Job `integration-db` ajouté (PostgreSQL 16).
4. **Le harnais E2E vivait hors du dépôt** — versé en `scripts/e2e-parallel-versions.py`,
   étendu d'une section « cohabitation » : 26 vérifications.
5. **Le pod de build portait un jeton d'API inutile** — `automountServiceAccountToken: false`.

Découverte incidente, antérieure aux trois passes : **la CI n'avait pas tourné depuis le
2026-07-15**. Ni 0.9.13 ni 0.9.14 n'ont déclenché de run ; la PR #17 est le premier
déclenchement en 11 jours, et elle est arrivée rouge sur un `bandit` B108 préexistant
(vérifié en le rejouant sur `7dbdf30`). Les mentions « checks » des relevés précédents
sont donc à lire avec cette réserve : il n'y avait pas de checks.

Tendance : ↓ — la duplication sémantique restante est éliminée, la preuve passe de
« déclarative » à « exécutée par la CI », et une dépendance morte sort de la base.

---

### Passe du 2026-07-26 (2/4) — après corrections de revue — branche `fix/experiment-directive-url`

Application des trois actions recommandées par la passe 1/4. Mêmes outils, mêmes
flags. La colonne « Avant » reprend la mesure de `7dbdf30`.

| Métrique | Outil (version) | Avant (`7dbdf30`) | Après | Delta |
|---|---|---|---|---|
| Duplication | jscpd 4.0.5 | 0,72 % (3 clones) | 0,72 % (3 clones) | = |
| Copies de la règle d'auto-complétion | grep | 4 | **1** | −3 |
| Complexité moyenne | radon 6.0.1 | B (5,27) | B (5,26) | −0,01 |
| Blocs CC > 15 | radon 6.0.1 | 30 | 30 | = |
| Indice de maintenabilité moyen | radon 6.0.1 | 65,0 | 65,0 | = |
| Couverture | pytest-cov 7.1.0 | 43 % (4 678 non couvertes) | 43 % (4 665 non couvertes) | −13 lignes non couvertes |
| Fonctions de test | grep | 286 | **298** | +12 |
| dont tests sur Postgres réel | grep | 4 fichiers | **5 fichiers** (8 tests de campagne) | +8 |
| Lint / SAST | ruff 0.15.5 / bandit 1.9.2 | 0 / High 0-Med 1 | 0 / High 0-Med 1 | = |
| `app/main.py` | wc | 5 283 l. | 5 279 l. | −4 |

Actions menées :

1. **URL de directive épinglée** — `_build_update_directive` construisait l'URL d'un
   bras d'expérimentation via `/catalog/<slug>/download`, qui résout
   `status = 'published'` : le device recevait la version main sous l'étiquette de la
   RC (checksum mismatch, puis re-update à chaque poll). Désormais route versionnée,
   repli sur le chemin brut. Constante `_CATALOG_KNOWN_EXT` extraite pour que la
   construction et le parse de l'URL partagent une source unique.
2. **Règle d'auto-complétion factorisée** — les 3 copies SQL de `app/main.py` appellent
   `campaigns_svc.autocomplete_superseded` (paramètre `campaign_type` ajouté pour couvrir
   le seul site qui variait) ; `_api_campaign_action` délègue entièrement à
   `update_campaign_status`. La précédence des campagnes se modifie en un seul endroit.
3. **Tests d'intégration sur Postgres réel** — `tests/test_experiment_campaigns_pg.py`
   (8 tests, marqués `integration`, transaction annulée en sortie) exécute la
   coexistence, la précédence, le scoping par plugin et le CHECK élargi. Vérifié par
   mutation : neutraliser la clause `COALESCE(is_experiment,…)` ou remplacer l'`ORDER BY`
   par `created_at` seul fait bien rougir la suite.

Tendance : ↓ — la duplication sémantique du cœur métier passe de 4 sites à 1, et la
règle centrale est désormais prouvée par exécution et non par comparaison de chaînes.

---

### Passe du 2026-07-26 (1/4) — commit `7dbdf30` (branche `fix/campaign-plugin-filter`, 0.9.14) — état zéro

Passe comparative **avant/après** : la colonne « Avant » mesure `main` (`715599a`,
0.9.12) dans un worktree séparé, la colonne « Après » mesure la branche. Les deux
mesures utilisent strictement les mêmes commandes.

| Métrique | Outil (version) | Commande exacte | Avant (`715599a`) | Après (`7dbdf30`) | Delta |
|---|---|---|---|---|---|
| Duplication | jscpd 4.0.5 | `npx jscpd@4 --min-tokens 50 --format python --reporters console app` | 0,74 % (3 clones, 46 l.) | 0,72 % (3 clones, 46 l.) | −0,02 pt |
| Complexité moyenne | radon 6.0.1 | `radon cc -s -a app` | B (5,20) — 570 blocs | B (5,27) — 576 blocs | +0,07 |
| Blocs CC > 15 | radon 6.0.1 | `radon cc -j app` + filtre `complexity > 15` | 29 | 30 | +1 |
| CC maximal | radon 6.0.1 | idem | 101 (`main.py:get_config`) | 101 (`main.py:get_config`) | = |
| Indice de maintenabilité moyen | radon 6.0.1 | `radon mi -j app` | 64,6 | 65,0 | +0,4 |
| Modules MI < 40 | radon 6.0.1 | idem | 4 | 5 (+ `services/db.py` 39,8) | +1 |
| Couverture | pytest-cov 7.1.0 | `pytest -m 'not integration' --cov=app --cov-report=term` | 42 % (8 003 stmts) | 43 % (8 137 stmts) | +1 pt |
| Fonctions de test | grep | `grep -rh "^def test_" tests/*.py \| wc -l` | 268 | 286 | +18 |
| Lint | ruff 0.15.5 | `ruff check .` | 0 | 0 | = |
| SAST | bandit 1.9.2 | `bandit -q -c pyproject.toml -r app` | High 0 / Med 1 | High 0 / Med 1 | = |
| Vulnérabilités dépendances | pip-audit 2.10.0 | `pip-audit -r requirements.txt` | 1 (pydantic-settings 2.14.1, GHSA-4xgf-cpjx-pc3j) | 1 (identique) | = |
| Volume (fichiers suivis) | git 2.x | `git ls-files \| wc -l` | 278 | 283 | +5 |
| Volume (lignes Python) | git + wc | `git ls-files '*.py' \| xargs wc -l \| tail -1` | 23 174 | 24 181 | +1 007 |
| Taille `app/main.py` | wc | `wc -l app/main.py` | 5 112 | 5 283 | +171 |
| Taille `app/admin/router.py` | wc | `wc -l app/admin/router.py` | 4 260 | 4 306 | +46 |

Réserves de mesure :

- `tests/test_queue_load.py::test_queue_load_smoke` échoue de façon
  **non déterministe** (seuil de performance) — observé en échec sous `--cov`
  sur les **deux** branches, donc sensible à l'environnement, pas une régression.
- jscpd ne détecte pas la duplication **sémantique** : la règle d'auto-complétion
  scopée existe en 4 exemplaires SQL variants (< 50 tokens chacun) invisibles pour
  l'outil. Le chiffre 0,72 % sous-estime la duplication réelle.

Actions menées : aucune (passe de mesure et de revue). Actions recommandées, par
rentabilité décroissante :

1. Factoriser les 4 copies de la règle d'auto-complétion scopée
   (`app/admin/services/campaigns.py:autocomplete_superseded` + `app/main.py`
   lignes ~3573, ~3670, ~3747) en un appel unique, sous test.
2. Ajouter un test marqué `integration` (Postgres réel, motif déjà présent dans
   `tests/test_plugin_installations_fk.py`) exerçant la précédence des campagnes
   et le CHECK élargi — aujourd'hui prouvés uniquement par assertions sur des
   fragments de chaîne SQL.
3. Sortir le domaine « catalogue » de `app/main.py` (MI 0,0 ; 5 283 lignes) vers
   un routeur dédié, à la manière de `app/admin/router.py`.

Tendance : → (état zéro ; la branche est neutre sur la duplication, légèrement
positive sur la couverture, légèrement négative sur la complexité et le volume).
