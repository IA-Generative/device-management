# Resume de contexte — Device Management

> Utilise ce fichier pour reprendre le travail dans un nouveau contexte Claude Code.
> Colle ce contenu au debut de la conversation.

---

## Projet

**Device Management** — Backend FastAPI de gestion de plugins pour les outils bureautiques.
Configuration centralisee, catalogue de plugins, deploiement progressif, telemetrie, relay securise.
Assiste par LLM pour l'analyse de packages et la generation de fiches catalogue.

**Repos** :
- `/Users/etiquet/Documents/GitHub/device-management` — serveur DM (FastAPI)
- `/Users/etiquet/Documents/GitHub/AssistantMiraiLibreOffice` — plugin LibreOffice (client)

## Etat actuel

| Element | Valeur |
|---------|--------|
| Image k8s | `0.3.0-catalog-v2-full` |
| Routes FastAPI | 113 |
| Tables DB | 26 (schema consolide `db/schema.sql`, pas de migrations) |
| Plugins en base | `mirai-libreoffice` (alias: libreoffice), `mirai-matisse` (alias: matisse) |
| Cluster Scaleway | `k8s-par-brave-bassi`, namespace `bootstrap` |
| Registry | `rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi` |
| Builder multi-arch | `dm-multiarch` (amd64+arm64) |
| Docker local | `deploy/docker/docker-compose.yml` (port 3001) |

## Architecture cle

- `device_name` = slug = identifiant universel du plugin (ex: `mirai-libreoffice`)
- `device_type` = type interne pour charger le template config (ex: `libreoffice`)
- `alias` = retrocompatibilite (ex: `libreoffice` → `mirai-libreoffice`)
- Les environnements sont **libres** (pas de CHECK constraint) : local, dev, int, prod, staging, dgx...
- `plugins.config_template` (JSONB) stocke le template dm-config.json par plugin
- Pipeline config : resolve slug/alias → merge default+profil → substitution ${{VAR}} → overrides catalogue → keycloak → access control → scrub secrets

## Ce qui a ete fait (commits recents)

1. **Fresh start DB** : schema.sql consolide (26 tables), plus de migrations
2. **Nettoyage** : supprime chrome/edge/firefox/misc, DEVICE_ALLOWLIST → `_resolve_device()`
3. **Pipeline config** : `_resolve_device()`, `_apply_catalog_overrides()`, `_check_plugin_access()`, injection device_name, alias tracking
4. **Config cleanup** : cles obsoletes supprimees (model, api_type, is_openwebui, etc.), `telemetryKey` re-ajoute aux secrets avec `_auth_notice`
5. **Rename** : `config/libreoffice/` → `config/mirai-libreoffice/`
6. **Catalog v2 complet** (steps 1-23) : CSS animations, slug intelligent, tags, logo upload, onglets admin (env, keycloak, acces, alias), dashboard (dispo + adoption chart), debug page, API JSON publique, endpoints monitoring Prometheus
7. **Onboarding decouple** : dm-config.json (default + sections env libres + placeholders ${{VAR}} + auto-completion)
8. **Plugin** : fix simplify_selection_max_tokens, keys_to_sync mis a jour

## Ce qui reste a faire

**Prompt a executer** : `prompts/prompt-catalog-onboarding.md`

Etat d'avancement de ce prompt :
- [x] `config_template JSONB` dans le schema
- [x] Environnements libres (VARCHAR(50), pas de CHECK)
- [x] Profils libres dans `get_config()` (accept any profile)
- [ ] `_apply_platform_defaults()` — auto-completion des placeholders ${{VAR}} dans les sections serveur
- [ ] Extraction de `dm-config.json` depuis le ZIP dans l'endpoint suggest
- [ ] `_build_config_from_template()` — merge default + section profil
- [ ] Modifier `_load_config_template()` pour charger depuis la DB en priorite
- [ ] `_strip_dm_config_from_zip()` — retirer dm-config.json du binaire distribue
- [ ] Stocker `config_template` a la creation du plugin
- [ ] Migration des 2 plugins existants (fichier → DB)
- [ ] Onglet "Configuration" dans la fiche plugin admin
- [ ] Tests de verification

**Prompt suivant** : `prompts/prompt-dm-config-packaging.md` (inclusion de dm-config.json dans le .oxt cote plugin)

## Fichiers cles a lire

| Fichier | Role |
|---------|------|
| `app/main.py` | API principale — `get_config()`, `_resolve_device()`, `_load_config_template()`, `_apply_catalog_overrides()` |
| `app/admin/router.py` | Routes admin — 104+ routes, suggest endpoint, deploy wizard, catalog CRUD |
| `app/admin/services/catalog.py` | Service catalogue — CRUD plugins, versions, overrides |
| `app/admin/services/keycloak.py` | Service keycloak — CRUD clients, export JSON |
| `db/schema.sql` | Schema unique consolide (26 tables, seed 2 plugins + alias) |
| `prompts/prompt-catalog-v2.md` | Spec complete du catalogue v2 (498 lignes, 12 sections, 23 etapes) |
| `prompts/prompt-catalog-onboarding.md` | Spec onboarding decouple + dm-config.json |
| `prompts/prompt-dm-config-packaging.md` | Spec inclusion dm-config.json dans le .oxt |

## Scripts utiles

```bash
# Build local (arm64, rapide)
./scripts/build-local.sh

# Build k8s (multi-arch + push)
./scripts/build-k8s.sh 0.3.1-feature-name

# Deploy Scaleway
./scripts/k8s/deploy.sh scaleway

# Reset DB Scaleway
kubectl -n bootstrap exec deploy/device-management -- python -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/bootstrap')
conn.autocommit = True
cur = conn.cursor()
cur.execute('DROP SCHEMA public CASCADE')
cur.execute('CREATE SCHEMA public')
with open('/app/db/schema.sql') as f: cur.execute(f.read())
print('OK')
conn.close()
"

# Tester config
curl -s 'http://localhost:3001/config/mirai-libreoffice/config.json?profile=dev' | python3 -m json.tool
```
