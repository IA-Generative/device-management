# ADR-0004 : Branches d'expérimentation — tester plusieurs versions d'un plugin en parallèle

**Date** : 2026-07-25
**Statut** : En vigueur (0.9.14, déployé intégration)
**Auteurs** : eric.tiquet + Claude Opus 4.8
**Portée** : coexistence de campagnes par cohorte + mode PIN (push, `app/main.py`,
`app/admin/services/campaigns.py`) ; canal `experimental` + `tag` + `hypotheses` au
catalogue (pull, `app/catalog/`) ; invalidation du cache binaire (`app/services/binaries.py`).

---

## Contexte

Besoin : tester **plusieurs versions d'un même plugin en même temps**, selon deux usages
qui ne se recouvrent pas.

- **Future version main (RC)** — la valider sur une **cohorte de testeurs** pendant que le
  rollout stable continue, en poussant automatiquement (auto-update via `/config`).
- **Versions martyres / prototypes** — builds jetables d'une branche d'expérimentation d'un
  principe, à **essayer manuellement** (pull opt-in), sans les imposer à personne.

Le système était conçu « **une seule campagne active par plugin, la plus récente gagne** » :
l'écriture auto-complétait toute campagne active du même plugin, la lecture prenait
`ORDER BY created_at DESC LIMIT 1`, et la comparaison de versions
(`_parse_version_tuple`) réduit toute version suffixée (`1.6.0-rc1`) à `(0,)` — donc aucun
update ne partait. Côté catalogue, seule la dernière version `published` était servable :
impossible d'exposer une build expérimentale sans en faire la « latest » de tout le monde.

## Décision

**Deux mécanismes complémentaires, additifs et gatés** (rien ne change tant qu'aucune donnée
d'expérimentation n'existe).

### 1. Push — campagnes coexistantes par cohorte

- `campaigns.is_experiment` / `campaigns.priority`.
- **Auto-complétion scopée** (`autocomplete_superseded`) : une release générale supersede
  les campagnes non-expé du plugin (sémantique historique) ; une expé ne remplace que
  l'expé **même-plugin/même-cohorte**. Ni l'une ni l'autre ne touche la classe opposée.
  Appliquée aussi aux transitions manuelles `activate/resume` (auparavant non gardées).
- **Précédence déterministe** : `ORDER BY (target_cohort_id IS NOT NULL) DESC, priority DESC,
  created_at DESC` — un bras ciblé bat le rollout général ; le device hors cohorte reste
  témoin sur le stable.
- **Mode PIN** : une campagne d'expé sert sa version cible dès que le device n'y est pas
  déjà, **sans** exiger cible > courante — c'est ce qui rend déployables les builds suffixées.

### 2. Pull — versions expérimentales au catalogue

- Statut `plugin_versions.status = 'experimental'` : servable par **version/tag précis** mais
  **jamais** comme « latest main ». Choix clé : élargir le CHECK plutôt qu'ajouter un booléen
  orthogonal — toutes les requêtes « dernière publiée » filtrent déjà `= 'published'`, donc
  `experimental` est exclu **par construction** (fail-safe : impossible d'oublier un filtre).
- `tag` (NULL = ligne main ; sinon branche, sert de tag d'URL) + `hypotheses` (JSONB : les
  questions clés testées, portées par la **version** donc partagées push/pull).
- Accès : `/catalog/<slug>?exp=<tag>` (section révélée par tag uniquement — invisible au
  public) et `/catalog/<slug>/download?tag=<tag>`.

### 3. Invalidation du cache binaire

Corollaire du cycle de vie des versions : purge/dépréciation supprime le binaire **à la
source** (PVC admin / S3) pour couper re-pull et raw-serve ; `POST /api/files/evict`
(self-healing) et `DELETE /api/files/<path>` évincent les caches locaux des pods.

## Alternatives écartées

- **Booléen `is_experimental` orthogonal au statut** (pull) : imposait d'ajouter
  `AND is_experimental = false` à ~6 requêtes « latest » — un oubli = fuite de l'expé comme
  version stable. Le statut dédié échoue en sécurité.
- **Imposer un schéma de version « supérieur à la baseline »** (au lieu du PIN) : fragile
  (tout suffixe s'effondre en `(0,)`) et interdit de tester des branches parallèles/inférieures.
- **Cohortes `percentage` pour partitionner** : deux cohortes % partagent le même hash
  `sha256(uuid)%100` → elles se **recouvrent** au lieu de diviser. Les expés utilisent des
  cohortes explicites (manuelle / email_pattern / groupe Keycloak).

## Conséquences

- **Compatibilité ascendante totale** : colonnes additives + CHECK élargi (sur-ensemble),
  migration idempotente (`003` + fixup `apply_schema` verrouillé). Comportement inchangé
  tant qu'`is_experiment`/`experimental`/`?exp=` ne sont pas utilisés.
- **À surveiller** : le client d'une expé doit renvoyer la version cible **exacte** (sinon
  re-update en boucle) ; ne pas utiliser de cohorte `percentage` pour un bras.
- **Non couvert (volontairement)** : pas de plateforme A/B (bras aléatoires disjoints, groupe
  témoin formel, comparaison de métriques par bras). Extension possible si le besoin émerge.

Détail opérateur : [../operations/mode-operatoire-campagnes.md](../operations/mode-operatoire-campagnes.md)
§3.3 (push) et §9 (pull + cache). Voir aussi [adr-0002](adr-0002-proxy-llm-relais.md)
(sécabilité) et le scoping par plugin (0.9.13, issue #14) dont ceci hérite.
