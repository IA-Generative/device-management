# Mode opératoire — Liens distribués aux applications (`links`, DM 0.9.24+)

Référence contrat : [plugin-dm-protocol-update-features.md §4.2 bis](../plugin-developer/plugin-dm-protocol-update-features.md).

## Le modèle en une phrase

Le DM diffuse dans `/config` une liste **résolue** de liens (`config.links`) que chaque
application **remplace en bloc** ; l'opérateur change l'adresse du portail ou de la
communauté bêta **sans republier de plugin**, et retire un lien en le masquant.

## D'où viennent les liens

Fusion **par `id`**, champ par champ, la couche la plus spécifique l'emporte :

| Couche | Où la modifier | Portée |
|---|---|---|
| `DM_LINKS` | values du chart `config.app.links`, puis onglet **Config** de l'admin (hot-reload) | toutes les applications |
| `DM_LINKS_BY_APP` | values `config.app.linksByApp`, puis onglet Config | une application (`device_type` : `matisse`, `pablo`, `libreoffice`…) |
| gabarit `dm-config.json` | paquet du plugin (`links` dans `default` et par profil) | un plugin, par profil |
| surcharge catalogue | fiche du plugin, variable d'environnement `links` (JSON) | un plugin, un profil |

Une couche n'a pas à répéter le lien entier : `{"id": "search-portal", "url": "https://…"}`
ne change que l'adresse ; `{"id": "beta-signup", "hidden": true}` retire le lien.

## Gestes courants (onglet Config admin — hot-reload, effectif au prochain poll `/config`)

| Besoin | Action |
|---|---|
| **Changer l'adresse du portail de recherche** pour toute la flotte | éditer `DM_LINKS`, champ `url` de l'entrée `search-portal` |
| **Fin de bêta** : retirer la mention « s'inscrire » | retirer `hint`/`hintUrl` de `search-portal` et masquer `beta-signup` et `beta-feedback` (`hidden: true`) ou les supprimer de `DM_LINKS` |
| **Ne pas montrer le portail dans LibreOffice** | `DM_LINKS_BY_APP` = `{"libreoffice": [{"id": "search-portal", "hidden": true}]}` |
| **Texte différent pour un plugin** | gabarit `dm-config.json` du plugin : `"links": [{"id": "search-portal", "label": "…"}]` dans le profil voulu, ou surcharge catalogue |
| **Revenir aux valeurs du chart** | bouton « réinitialiser » de la clé dans l'onglet Config |

La valeur saisie doit être un JSON valide (refusé sinon). Une **entrée** hors contrat
(id non kebab-case, `url` sans `https://`, `label` manquant) est retirée de la liste
servie et journalisée (`device-management.links`), la liste reste servie.

## Vérifier

```bash
curl -sS "$DM/config/matisse/config.json?profile=int" -H 'X-Client-UUID: test' | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["config"]["links"], indent=2, ensure_ascii=False))'
```

`X-Client-UUID` contourne le cache de 60 s. La clé est **toujours** présente (`[]` si rien).

## Pièges connus

- Un lien retiré côté DM disparaît du poste au poll suivant : c'est voulu (zéro lien
  fantôme), ne pas compter sur une valeur « par défaut » côté plugin.
- Le plugin ne rend que les `id` qu'il connaît : ajouter une entrée avec un id nouveau
  n'affiche rien tant que le plugin ne l'implémente pas — mais ne casse rien.
- `linksByApp` est indexé par `device_type`, pas par slug (`matisse`, pas `mirai-matisse`).
