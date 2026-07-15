# Mode opératoire — Feature flags (v2, DM 0.9.2+)

Référence protocole : `docs/plugin-developer/plugin-dm-protocol-update-features.md` §4.4.
Rapport de la refonte : repo privé, `docs/reports/feature-flags-refonte.md`.

## Le modèle en une phrase

Les **défauts** vivent dans le config template du plugin (dm-config.json, par profil,
deep-mergé) ; l'admin n'ajoute que des **overrides de cohorte** ; le serveur diffuse
l'objet **résolu** (`features`) que le client **remplace en bloc** — un flag supprimé
disparaît du parc au prochain poll.

## Ce qui est automatique

- **Import** : à chaque chemin d'import d'un plugin (api deploy, deploy/create admin,
  édition du config-template, migration, création catalogue, upload de version — les
  **six** passent par le wrapper unique `flags.safe_reconcile_catalog`, cf. règle
  mainteneur dans [developer-readme.md](developer-readme.md)), le catalogue est
  réconcilié depuis le dm-config.json embarqué — flags ajoutés/conservés/
  réactivés, disparus **marqués « orphelin »** (badge dans `/admin/flags`, plus jamais
  diffusés, jamais supprimés automatiquement). Le diff est renvoyé par l'API du deploy
  et tracé dans le journal d'audit (`flag.reconcile`).
- **Valeur défaut (indicative)** de la liste `/admin/flags` : recopie du `default` du
  template au dernier import. Les valeurs réelles sont **par profil**.

## Tri-état : transparent / forcé ON / forcé OFF (DM 0.9.11+)

Le `default_value` du catalogue est **tri-état** — c'est la surcharge admin appliquée
dans `/config` :

| État | Valeur en base | Effet dans `/config` |
|---|---|---|
| **transparent** | `NULL` | pas de surcharge : le template (défaut puis profil), puis les cohortes, décident |
| **forcé ON** | `true` | la feature est servie ON même si le template la livre OFF |
| **forcé OFF** | `false` | la feature est servie OFF même si le template la livre ON |

- Un flag est **enregistré transparent** au premier import ; l'admin le force ensuite
  ON/OFF depuis `/admin/flags`. La réconciliation à l'import **ne réécrit jamais** un
  état posé par l'admin.
- Ordre de résolution : défaut du template → profil du template → **forcé catalogue**
  → **override de cohorte** (plus spécifique, il l'emporte sur le forcé).
- Le forcé respecte la `min_plugin_version` du flag (client sous le minimum, ou qui
  n'annonce pas sa version : non servi).

## Gestes admin (`/admin/flags`)

- **Créer un flag** : nom snake_case + **plugin** (scope ; « Global » = tous) +
  **version minimale** optionnelle (vide = toutes les versions — gate fail-safe : un
  client qui n'annonce pas sa version n'est pas servi).
- **Forcer / rendre transparent** : sélecteur d'état sur la page du flag
  (« Transparent (le template décide) » / « Forcé ON » / « Forcé OFF »), disponible
  aussi à la création ; changement audité (`flag.update`).
- **Override de cohorte** (page détail) : valeur + `min_plugin_version` optionnelle.
  Entre cohortes contradictoires, **false gagne**. L'override prime sur le profil du
  template — c'est le kill-switch.
- **Supprimer un flag** : bouton « Supprimer » (purge le flag ET ses overrides,
  audité `flag.delete`). Réservé aux orphelins vérifiés ou aux erreurs de saisie.

## Côté client (pour diagnostic)

- Pref `extensions.IAssistant.featureToggles` = **base locale** (seedée depuis le
  prefs.js du plugin ; modifiable par l'utilisateur via « Gérer les fonctionnalités »).
- Pref `extensions.IAssistant.featureTogglesOverride` = **objet `features` du dernier
  /config**, remplacé en bloc (persiste offline).
- État effectif = base ⊕ override, recalculé à chaque lecture. Un flag imposé par le
  DM reste prioritaire sur le choix utilisateur tant que l'override est présent.

## Pièges connus

- Un override sur un flag **orphelin** n'est jamais diffusé (pas de résurrection).
- `?profile=` change les valeurs : vérifier avec le bon profil
  (`curl …/config/<device>/config.json?profile=int` → champ `features`).
- Le `/config` sans en-têtes d'enrichissement est mis en cache 60 s : pour tester un
  changement, ajouter `X-Client-UUID` (bypass).
