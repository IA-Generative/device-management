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
