# Bug : les campagnes de mise à jour ne servent plus d'update directive

## Contexte

Le système Device Management (DM) gère les mises à jour du plugin LibreOffice MIrAI.
Le endpoint `/config/{device}/config.json` retourne un champ `"update"` quand une campagne
active cible une version supérieure à celle du plugin (`X-Plugin-Version` header).

## Symptôme

Après un `POST /api/plugins/mirai-libreoffice/deploy` réussi (campaign_id=17, strategy=immediate),
**aucun device ne reçoit de directive update**, quelle que soit la version du plugin :

```bash
# Tous retournent "update": null
curl -H "X-Plugin-Version: 0.0.0.1.0" .../config/mirai-libreoffice/config.json?profile=int
curl -H "X-Plugin-Version: 0.0.1.0.3" .../config/mirai-libreoffice/config.json?profile=int
curl (sans header) .../config/mirai-libreoffice/config.json?profile=int
curl .../config/libreoffice/config.json?profile=int
```

Pourtant le catalog fonctionne :
```bash
curl -L .../catalog/mirai-libreoffice/download  # → 200, OXT valide v0.0.1.0.4
```

## Historique

- Campagnes 5-12 : ont fonctionné puis se sont "brûlées" (devices marqués installed/error)
- Campagne 13 : a fonctionné brièvement puis plus rien
- Campagne 17 : créée via `/api/plugins/mirai-libreoffice/deploy` → retour `{"ok":true}` mais ne sert aucune update

## Hypothèses

1. **Le endpoint `/api/plugins/{slug}/deploy` auto-complète TOUTES les campagnes** y compris celle qu'il vient de créer
2. **`_resolve_active_campaign` ne trouve pas la campagne 17** — peut-être un filtre sur `status`, `device_type`, `profile`, ou une jointure qui exclut les nouvelles campagnes
3. **La campagne 17 est créée avec un status non-actif** (ex: `completed`, `draft` au lieu de `active`)
4. **Le `device_type` ne matche pas** — la campagne est créée pour `libreoffice` mais la résolution passe par `mirai-libreoffice`

## Fichiers à investiguer

### `app/main.py`

1. **`/api/plugins/{slug}/deploy`** — Le endpoint unifié de déploiement. Vérifier :
   - Quel status est assigné à la nouvelle campagne ?
   - Est-ce que `_auto_complete_campaigns()` est appelé APRÈS la création et complète aussi la nouvelle ?
   - Quel `device_type` est assigné à la campagne ?

2. **`_resolve_active_campaign(cur, device_cohort_ids, device_type, platform_version)`** — Chercher :
   - Quelle requête SQL est exécutée ?
   - Quels filtres sur `status` (doit être `active`) ?
   - Quel filtre sur `device_type` ? Est-ce `libreoffice` ou `mirai-libreoffice` ?
   - Y a-t-il un filtre `profile` ?

3. **`_build_update_directive(plugin_version, campaign, client_uuid, device_name)`** — Vérifier :
   - Le guard `plugin_version in ("unknown", "0", "")` ne bloque pas des versions valides
   - La comparaison de version `_parse_version_tuple` fonctionne avec les versions 5 segments (0.0.1.0.4)

4. **Endpoint `/config/{device}/config.json`** (fonction `get_config`) — Tracer :
   - `device_type` résolu (via `_resolve_device`)
   - `plugin_version` extrait du header
   - Résultat de `_resolve_active_campaign`
   - Résultat de `_build_update_directive`

### `db/schema.sql`

- Table `campaigns` : colonnes `status`, `device_type`, `artifact_version`, `rollout_config`
- Vérifier si campaign 17 existe et son status

## Comment reproduire

```bash
# 1. Vérifier que la campagne existe en DB
# (nécessite accès psql ou l'admin API)
SELECT id, status, device_type, artifact_version, created_at
FROM campaigns WHERE id = 17;

# 2. Tester le endpoint config
curl -v -H "X-Plugin-Version: 0.0.0.1.0" \
  https://<SCALEWAY_HOSTNAME>/config/mirai-libreoffice/config.json?profile=int

# 3. Ajouter des logs temporaires dans _resolve_active_campaign
# pour voir la requête SQL et son résultat
```

## Résultat attendu

Un `POST /api/plugins/{slug}/deploy` avec `strategy=immediate` doit créer une campagne
qui sert immédiatement une directive `"update"` à tous les devices dont la version est
inférieure à la `target_version`.

## Contraintes

- Ne pas casser le endpoint `/config` qui sert aussi la configuration LLM
- Ne pas modifier le format de la réponse (le plugin parse `update.action`, `update.target_version`, `update.artifact_url`, `update.checksum`)
- Les campagnes précédentes doivent rester complétées
