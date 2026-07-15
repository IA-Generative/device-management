#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Matrice E2E feature-flags v2 — smoke + bout-en-bout RÉELS (Docker requis).
#
# Monte le harness deploy/docker (profil postgres-local + override e2e),
# seed un plugin 'matisse-e2e' + son dm-config.json par profil, puis déroule
# les 8 combinaisons du plan de refonte :
#   1. base seule            → features = template résolu
#   2. per-profil            → int search=true / prod search=false
#   3. override cohorte      → la cohorte gagne sur le profil
#   4. gating                → min_plugin_version (trop vieux / inconnu / ok)
#   5. +flag au bump         → catalogue + /config
#   6. −flag au bump         → zéro fantôme + orphelin marqué
#   7. delete_flag           → route admin + overrides purgés
#   8. deep vs superficiel   → les flags du default survivent au profil
# + contract test plugin (Node, si ../mirai-assistant présent) : le payload
#   RÉEL de /config passé dans modules/feature-flags.js (recompute + retrait).
#
# Usage :  scripts/e2e-feature-flags-matrix.sh [--keep-db]
#   --keep-db : ne pas repartir d'un volume Postgres vierge (défaut : reset).
# Le stack reste up à la fin (debug) : docker compose ... down -v pour nettoyer.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")/.."

# -p dm-e2e : projet dédié — sans lui le projet s'appellerait « docker »
# (dossier) et son réseau par défaut « docker_default » entrerait en collision
# avec le réseau EXTERNE telemetry du même nom (que `down` détruirait).
COMPOSE=(docker compose -p dm-e2e -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.e2e.yml --profile postgres-local)
# Ports dédiés E2E : ne pas marcher sur un stack dev déjà up (8089/5433).
export DM_PORT=${DM_PORT:-18089}
export POSTGRES_PORT=${POSTGRES_PORT:-15433}
BASE_URL=${BASE_URL:-http://localhost:$DM_PORT}
ADMIN_TOKEN=e2e-admin-token
SLUG=matisse-e2e
TMPDIR_E2E=$(mktemp -d)
trap 'rm -rf "$TMPDIR_E2E"' EXIT

PASS=0; FAIL=0; RESULTS=()
check() { # check <nom> <obtenu> <attendu>
  if [[ "$2" == "$3" ]]; then
    PASS=$((PASS+1)); RESULTS+=("PASS  $1"); echo "  ✓ $1"
  else
    FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1 (attendu: $3, obtenu: $2)"); echo "  ✗ $1 — attendu: $3, obtenu: $2"
  fi
}

psql_q() { "${COMPOSE[@]}" exec -T postgres-local psql -U dev -d bootstrap -tAc "$1"; }

# get_feature <profile> <clé> [headers curl supplémentaires...]
# Imprime la valeur JSON de features[clé] ("__absent__" si absente).
get_feature() {
  local profile="$1" key="$2"; shift 2
  curl -sf "$BASE_URL/config/config.json?profile=$profile&device=$SLUG" "$@" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get("features",{}).get(sys.argv[1],"__absent__")))' "$key"
}

# make_zip <chemin.zip> <version> <template-json>
make_zip() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys, zipfile
out, version, tpl = sys.argv[1], sys.argv[2], sys.argv[3]
json.loads(tpl)  # valide
with zipfile.ZipFile(out, "w") as z:
    z.writestr("manifest.json", json.dumps({"version": version}))
    z.writestr("dm-config.json", tpl)
PY
}

deploy_zip() { # deploy_zip <chemin.zip> → JSON de réponse
  curl -sf -X POST "$BASE_URL/api/plugins/$SLUG/deploy" \
    -H "X-Admin-Token: $ADMIN_TOKEN" \
    -F "binary=@$1" -F "strategy=immediate" -F "urgency=low"
}

TPL_V1='{"configVersion":1,"default":{"enabled":true,"featureToggles":{"composePromptPanel":true,"dailySummary":true,"calendarDetector":true,"threadSummary":true,"search":false}},"int":{"featureToggles":{"search":true}},"prod":{}}'
TPL_V2='{"configVersion":2,"default":{"enabled":true,"featureToggles":{"composePromptPanel":true,"dailySummary":true,"calendarDetector":true,"threadSummary":true,"search":false,"newFeature":true}},"int":{"featureToggles":{"search":true}},"prod":{}}'
TPL_V3="$TPL_V1"  # v3 = retrait de newFeature

# ── 0. Harness up ───────────────────────────────────────────────────────────
echo "── Harness Docker (profil postgres-local) ──"
# Réseaux déclarés external dans le compose de base : requis à la création.
docker network inspect owui-net >/dev/null 2>&1 || docker network create owui-net >/dev/null
docker network inspect docker_default >/dev/null 2>&1 || docker network create docker_default >/dev/null
if [[ "${1:-}" != "--keep-db" ]]; then
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
fi
"${COMPOSE[@]}" up --build -d postgres-local device-management

echo -n "Attente readiness"
for i in $(seq 1 60); do
  if curl -sf "$BASE_URL/healthz" >/dev/null 2>&1; then echo " OK"; break; fi
  [[ $i == 60 ]] && { echo " ÉCHEC (healthz)"; "${COMPOSE[@]}" logs --tail 50 device-management; exit 1; }
  echo -n "."; sleep 2
done
# La DB doit être migrée (apply_schema au startup)
for i in $(seq 1 30); do
  psql_q "SELECT 1 FROM information_schema.columns WHERE table_name='feature_flags' AND column_name='plugin_slug'" | grep -q 1 && break
  [[ $i == 30 ]] && { echo "Schéma flags v2 absent (plugin_slug)"; exit 1; }
  sleep 1
done

# ── Seed : plugin catalogue ────────────────────────────────────────────────
psql_q "INSERT INTO plugins (slug, name, device_type, status)
        VALUES ('$SLUG', 'Matisse E2E', 'thunderbird', 'active')
        ON CONFLICT DO NOTHING;" >/dev/null

# Import v1 (crée config_template + catalogue de flags)
make_zip "$TMPDIR_E2E/v1.xpi" "0.13.7" "$TPL_V1"
R_V1=$(deploy_zip "$TMPDIR_E2E/v1.xpi")
echo "deploy v1: $(echo "$R_V1" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["version"], d.get("feature_flags",{}).get("added"))')"

UUID_H=(-H "X-Client-UUID: 00000000-0000-4000-8000-00000000e2e0")

echo "── C1. base seule (profil int, sans cohorte) ──"
check "C1 features.search (int, template résolu)"          "$(get_feature int search)" "true"
check "C1 features.composePromptPanel présent"             "$(get_feature int composePromptPanel)" "true"
check "C1 features.dailySummary présent"                   "$(get_feature int dailySummary)" "true"

echo "── C8. deep-merge vs superficiel ──"
# Avec un merge superficiel, featureToggles du profil int ({search:true})
# aurait EFFACÉ les 4 autres clés du default.
check "C8 threadSummary survit au profil int (deep-merge)" "$(get_feature int threadSummary)" "true"
check "C8 calendarDetector survit au profil int"           "$(get_feature int calendarDetector)" "true"

echo "── C2. per-profil ──"
check "C2 int  → search=true"  "$(get_feature int  search "${UUID_H[@]}")" "true"
check "C2 prod → search=false" "$(get_feature prod search "${UUID_H[@]}")" "false"

echo "── C5. +flag au bump (v2 ajoute newFeature) ──"
make_zip "$TMPDIR_E2E/v2.xpi" "0.13.8" "$TPL_V2"
R_V2=$(deploy_zip "$TMPDIR_E2E/v2.xpi")
# added au 1er passage, reactivated si la DB est réutilisée (--keep-db :
# newFeature existe déjà, orphelin du passage précédent)
check "C5 diff import v2 : newFeature ajouté/réactivé" \
  "$(echo "$R_V2" | python3 -c 'import json,sys;d=json.load(sys.stdin)["feature_flags"];print(json.dumps(sorted(d["added"]+d["reactivated"])))')" '["newFeature"]'
check "C5 catalogue scopé = 6 flags" "$(psql_q "SELECT count(*) FROM feature_flags WHERE plugin_slug='$SLUG'")" "6"
check "C5 /config expose newFeature=true" "$(get_feature int newFeature "${UUID_H[@]}")" "true"

echo "── C6. −flag au bump (v3 retire newFeature) ──"
make_zip "$TMPDIR_E2E/v3.xpi" "0.13.9" "$TPL_V3"
R_V3=$(deploy_zip "$TMPDIR_E2E/v3.xpi")
check "C6 diff import v3 orphaned=[newFeature]" \
  "$(echo "$R_V3" | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["feature_flags"]["orphaned"]))')" '["newFeature"]'
check "C6 zéro fantôme dans /config" "$(get_feature int newFeature "${UUID_H[@]}")" '"__absent__"'
check "C6 orphelin MARQUÉ (pas supprimé)" \
  "$(psql_q "SELECT deprecated FROM feature_flags WHERE plugin_slug='$SLUG' AND name='newFeature'")" "t"

echo "── C3. override cohorte ──"
psql_q "INSERT INTO cohorts (name, type, config) VALUES ('e2e-cohorte', 'manual', '{}')
        ON CONFLICT DO NOTHING;" >/dev/null
COHORT_ID=$(psql_q "SELECT id FROM cohorts WHERE name='e2e-cohorte'")
psql_q "INSERT INTO cohort_members (cohort_id, identifier_type, identifier_value)
        VALUES ($COHORT_ID, 'email', 'e2e@test.gouv.fr') ON CONFLICT DO NOTHING;" >/dev/null
FLAG_ID=$(psql_q "SELECT id FROM feature_flags WHERE plugin_slug='$SLUG' AND name='search'")
psql_q "INSERT INTO feature_flag_overrides (feature_id, cohort_id, value)
        VALUES ($FLAG_ID, $COHORT_ID, false)
        ON CONFLICT (feature_id, cohort_id) DO UPDATE SET value=false, min_plugin_version=NULL;" >/dev/null
EMAIL_H=(-H "X-User-Email: e2e@test.gouv.fr")
check "C3 cohorte (search=false) gagne sur profil int (true)" \
  "$(get_feature int search "${EMAIL_H[@]}")" "false"
check "C3 hors cohorte : profil int inchangé" \
  "$(get_feature int search "${UUID_H[@]}")" "true"

echo "── C4. gating min_plugin_version ──"
psql_q "UPDATE feature_flag_overrides SET min_plugin_version='9.9.9' WHERE feature_id=$FLAG_ID;" >/dev/null
check "C4 plugin 0.13.7 < gate 9.9.9 → override ignoré" \
  "$(get_feature int search "${EMAIL_H[@]}" -H 'X-Plugin-Version: 0.13.7')" "true"
check "C4 version inconnue → fail-safe, override ignoré" \
  "$(get_feature int search "${EMAIL_H[@]}")" "true"
psql_q "UPDATE feature_flag_overrides SET min_plugin_version='0.1.0' WHERE feature_id=$FLAG_ID;" >/dev/null
check "C4 plugin 0.13.7 ≥ gate 0.1.0 → override appliqué" \
  "$(get_feature int search "${EMAIL_H[@]}" -H 'X-Plugin-Version: 0.13.7')" "false"

echo "── C9. détail flag admin (régression ffo.updated_at) ──"
# Avant le fix, /admin/flags/{id} explosait en UndefinedColumn (colonne
# updated_at absente de feature_flag_overrides mais SELECTée + ON CONFLICTée).
HTTP_DETAIL=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/admin/flags/$FLAG_ID")
check "C9 GET /admin/flags/{id} → 200" "$HTTP_DETAIL" "200"

echo "── C10. upsert override via route admin (ON CONFLICT … updated_at) ──"
HTTP_OV1=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/admin/flags/$FLAG_ID/overrides" \
  --data-urlencode "cohort_id=$COHORT_ID" --data-urlencode "value=false" --data-urlencode "min_plugin_version=0.1.0")
HTTP_OV2=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/admin/flags/$FLAG_ID/overrides" \
  --data-urlencode "cohort_id=$COHORT_ID" --data-urlencode "value=false" --data-urlencode "min_plugin_version=0.1.0")
check "C10 create override → 303" "$HTTP_OV1" "303"
check "C10 re-create (chemin ON CONFLICT updated_at) → 303" "$HTTP_OV2" "303"

echo "── C11. création de flag admin : plugin + version min ──"
psql_q "DELETE FROM feature_flags WHERE name='e2e_gated_flag';" >/dev/null
HTTP_CREATE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/admin/flags" \
  --data-urlencode "name=e2e_gated_flag" --data-urlencode "default_value=false" \
  --data-urlencode "plugin_slug=$SLUG" --data-urlencode "min_plugin_version=0.14.0")
check "C11 POST /admin/flags (plugin+version) → 303" "$HTTP_CREATE" "303"
check "C11 flag scopé + version min persistés" \
  "$(psql_q "SELECT plugin_slug || '|' || min_plugin_version FROM feature_flags WHERE name='e2e_gated_flag'")" \
  "$SLUG|0.14.0"

echo "── C12. heartbeat plugin_installations (/config enrichi) ──"
# La table n'était JAMAIS écrite avant le fix : onglet Installations toujours vide.
curl -sf "$BASE_URL/config/config.json?profile=int&device=$SLUG" "${UUID_H[@]}" \
  -H 'X-Plugin-Version: 0.13.7' >/dev/null
check "C12 installation enregistrée (client e2e)" \
  "$(psql_q "SELECT count(*) FROM plugin_installations pi JOIN plugins p ON p.id=pi.plugin_id WHERE p.slug='$SLUG' AND pi.client_uuid='00000000-0000-4000-8000-00000000e2e0'")" "1"
check "C12 version vue au dernier contact" \
  "$(psql_q "SELECT installed_version FROM plugin_installations WHERE client_uuid='00000000-0000-4000-8000-00000000e2e0' ORDER BY last_seen_at DESC LIMIT 1")" "0.13.7"

echo "── C13. dashboard adoption : toggle Appareils/Utilisateurs ──"
# Seed : 2 postes du MÊME agent sur $SLUG + 1 poste d'un autre agent sur un
# 2e plugin → device=3, user=2, séries par plugin = {$SLUG: 2, e2e-autre-plugin: 1}.
psql_q "DELETE FROM provisioning WHERE email LIKE 'e2e-adopt%';" >/dev/null
psql_q "INSERT INTO provisioning (email, device_name, client_uuid, status, encryption_key) VALUES
        ('e2e-adopt-a@test.gouv.fr', '$SLUG', '11111111-1111-4111-8111-111111111101', 'ENROLLED', 'k'),
        ('e2e-adopt-a@test.gouv.fr', '$SLUG', '11111111-1111-4111-8111-111111111102', 'ENROLLED', 'k'),
        ('e2e-adopt-b@test.gouv.fr', 'e2e-autre-plugin', '11111111-1111-4111-8111-111111111103', 'ENROLLED', 'k');" >/dev/null
adoption() { curl -sf "$BASE_URL/admin/api/adoption?period=1M&mode=$1" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["mode"], d["summary"]["total"])'; }
read -r DEV_MODE DEV_N <<<"$(adoption device)"
read -r USR_MODE USR_N <<<"$(adoption user)"
check "C13 mode=device écho dans la réponse" "$DEV_MODE" "device"
check "C13 mode=user écho dans la réponse"   "$USR_MODE" "user"
check "C13 device=3 (2 postes agent A + 1 agent B)" "$DEV_N" "3"
check "C13 user=2 (agrégation par email)"           "$USR_N" "2"
check "C13 mode invalide → repli device (allow-list)" \
  "$(curl -sf "$BASE_URL/admin/api/adoption?mode=zzz" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mode"])')" "device"

echo "── C14. courbes par plugin + métriques cohérentes ──"
# Séries par plugin : dernier point de chaque slug + cohérence somme = total.
check "C14 séries par plugin (dernier point par slug)" \
  "$(curl -sf "$BASE_URL/admin/api/adoption?period=1M&mode=device" | python3 -c '
import json, sys
d = json.load(sys.stdin)
last = {s["slug"]: s["points"][-1]["enrolled"] for s in d["series"]}
total_last = d["timeseries"][-1]["enrolled"]
print(json.dumps(last, sort_keys=True), "sum_ok" if sum(last.values()) == total_last else "sum_ko")')" \
  "{\"e2e-autre-plugin\": 1, \"$SLUG\": 2} sum_ok"
# actifs (7j) ≤ 100 % : numérateur = heartbeat plugin_installations (même
# population que le dénominateur), plus jamais device_connections pollué.
check "C14 active_pct ≤ 100" \
  "$(curl -sf "$BASE_URL/admin/api/adoption?period=1M&mode=device" | python3 -c 'import json,sys;print(json.load(sys.stdin)["summary"]["active_pct"] <= 100)')" "True"
# Tuile métriques : 2 valeurs (appareils + interactions).
check "C14 fragment métriques expose les interactions" \
  "$(curl -sf "$BASE_URL/admin/api/metrics" | grep -c 'interactions')" "1"
# Token télémétrie : claim cuid = identité stable du client. Vérifié via
# /telemetry/token (même code de mint que /config, mais la telemetryKey du
# /config est scrubée sans credentials relay).
check "C14 token télémétrie embarque cuid (identité stable)" \
  "$(curl -sf "$BASE_URL/telemetry/token?profile=int&device=$SLUG" "${UUID_H[@]}" | python3 -c '
import base64, json, sys
tok = json.load(sys.stdin).get("telemetryKey") or ""
if not tok: print("no-token"); raise SystemExit
p64 = tok.split(".", 1)[0]; p64 += "=" * (-len(p64) % 4)
print(json.loads(base64.urlsafe_b64decode(p64)).get("cuid", "absent"))')" \
  "00000000-0000-4000-8000-00000000e2e0"

echo "── C15. journal d'audit : filtres live, autocomplétion, scroll infini ──"
# Le harness a déjà généré des entrées d'audit (flag.create/reconcile/delete…).
AUDIT_PAGE=$(curl -sf "$BASE_URL/admin/audit")
check "C15 page 200 + datalists d'autocomplétion dynamiques" \
  "$(echo "$AUDIT_PAGE" | grep -c 'dl-audit-actions')" "2"
check "C15 select ressources peuplé depuis les données (flag)" \
  "$(echo "$AUDIT_PAGE" | grep -cE '<option value="flag"')" "1"
# Fragment HTMX (filtres live) : tableau seul, sans <html>
FRAG=$(curl -sf "$BASE_URL/admin/audit?action=flag.reconcile" -H "HX-Request: true")
check "C15 fragment HTMX = tableau seul" \
  "$(echo "$FRAG" | grep -c 'id="audit-table"'):$(echo "$FRAG" | grep -c '<html')" "1:0"
check "C15 filtre action appliqué (flag.reconcile présents)" \
  "$(echo "$FRAG" | grep -c 'flag.reconcile' | awk '{print ($1>0)?"oui":"non"}')" "oui"
# Recherche dans les détails (q) : le diff d'import contient "orphaned"
check "C15 filtre q dans les détails (payload)" \
  "$(curl -sf "$BASE_URL/admin/audit?q=newFeature" -H 'HX-Request: true' | grep -c 'flag.reconcile' | awk '{print ($1>0)?"oui":"non"}')" "oui"
# Scroll infini : partial=rows accepté (sentinel) — page hors bornes = vide sans erreur
check "C15 partial=rows (sentinel scroll infini) → 200" \
  "$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/admin/audit?partial=rows&page=99")" "200"
# Export CSV filtré par période
check "C15 export CSV avec période → 200 + en-tête" \
  "$(curl -sf "$BASE_URL/admin/audit/export?period=24h" | head -1 | grep -c horodatage)" "1"
# Colonne Plugin dérivée : flag.reconcile (ressource plugin:$SLUG) doit
# afficher le slug ; le filtre plugin= restreint aux entrées du plugin.
check "C15 colonne Plugin remplie (flag.reconcile → $SLUG)" \
  "$(curl -sf "$BASE_URL/admin/audit?action=flag.reconcile" -H 'HX-Request: true' | grep -c "<td class=\"audit-res\">$SLUG</td>" | awk '{print ($1>0)?"oui":"non"}')" "oui"
check "C15 filtre plugin=$SLUG → lignes ; plugin=inexistant → aucune" \
  "$(curl -sf "$BASE_URL/admin/audit?plugin=$SLUG" -H 'HX-Request: true' | grep -c 'flag.reconcile' | awk '{print ($1>0)?"oui":"non"}'):$(curl -sf "$BASE_URL/admin/audit?plugin=zzz-inexistant" -H 'HX-Request: true' | grep -c 'Aucune entrée')" "oui:1"
# Fix long terme : plugin_slug PERSISTÉ dans la table à l'écriture (pas
# seulement dérivé à la lecture) — l'historique survivra aux suppressions.
check "C15 plugin_slug écrit EN COLONNE (flag.create le plus récent)" \
  "$(psql_q "SELECT plugin_slug FROM admin_audit_log WHERE action='flag.create' ORDER BY created_at DESC LIMIT 1")" "$SLUG"
# Repli lecture : une ligne legacy (colonne NULL, ressource plugin:*) reste
# trouvée par le filtre (COALESCE colonne → dérivation).
psql_q "INSERT INTO admin_audit_log (actor_email, actor_sub, action, resource_type, resource_id)
        VALUES ('legacy@test', 'legacy', 'legacy.action', 'plugin', '$SLUG');" >/dev/null
check "C15 ligne legacy (colonne NULL) trouvée via le repli" \
  "$(curl -sf "$BASE_URL/admin/audit?plugin=$SLUG&action=legacy.action" -H 'HX-Request: true' | grep -c 'legacy.action' | awk '{print ($1>0)?"oui":"non"}')" "oui"

echo "── C7. delete_flag (route admin, autologin dev) ──"
HTTP_DEL=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE_URL/admin/flags/$FLAG_ID")
check "C7 DELETE /admin/flags/{id} → 303" "$HTTP_DEL" "303"
check "C7 flag purgé du catalogue" "$(psql_q "SELECT count(*) FROM feature_flags WHERE id=$FLAG_ID")" "0"
check "C7 overrides purgés" "$(psql_q "SELECT count(*) FROM feature_flag_overrides WHERE feature_id=$FLAG_ID")" "0"
check "C7 /config retombe sur le template (int → true)" \
  "$(get_feature int search "${EMAIL_H[@]}" -H 'X-Plugin-Version: 0.13.7')" "true"

# ── Contract test plugin (Node) : payload RÉEL → modules/feature-flags.js ──
PLUGIN_DIR="../mirai-assistant/matisse/thunderbird/60.9.1"
if [[ -f "$PLUGIN_DIR/modules/feature-flags.js" ]] && command -v node >/dev/null; then
  echo "── Contract plugin (recompute + retrait en bloc) ──"
  FEATURES_JSON=$(curl -sf "$BASE_URL/config/config.json?profile=int&device=$SLUG" "${UUID_H[@]}" \
    | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["features"]))')
  CONTRACT=$(node - "$PLUGIN_DIR/modules/feature-flags.js" "$FEATURES_JSON" <<'JS'
const ff = require(process.argv[2]);
const dm = process.argv[3];
const base = JSON.stringify({composePromptPanel:true,dailySummary:true,calendarDetector:true,threadSummary:true,search:false});
const eff = ff.computeEffectiveToggles(base, dm);          // /config appliqué
const after = ff.computeEffectiveToggles(base, "{}");      // DM retire tout → retrait en bloc
const ok = eff.search === true && after.search === false && !("newFeature" in after);
console.log(ok ? "ok" : "ko " + JSON.stringify({eff, after}));
JS
)
  check "Contract plugin : recompute base⊕features réel + retrait" "$CONTRACT" "ok"
fi

echo
echo "═══ RÉSULTATS MATRICE ═══"
printf '%s\n' "${RESULTS[@]}"
echo "─────────────────────────"
echo "PASS=$PASS FAIL=$FAIL"
echo "(stack laissé up — nettoyage : ${COMPOSE[*]} down -v)"
[[ $FAIL == 0 ]]
