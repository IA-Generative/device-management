#!/usr/bin/env bash
# verif-version.sh — l'image (ou l'application qui tourne) dit-elle bien quelle version elle est ?
#
# Convention ADR-0004 MirAI next, format Dockerflow : /app/version.json dans l'image, servi
# sur GET /__version__. Ce contrôle est le même quel que soit le langage ; c'est ce qu'on
# recopie mal d'un dépôt à l'autre, d'où ce script. Dépendances : jq ; docker (mode --image) ;
# curl (mode --url). Il ne construit PAS l'image — la commande de build dépend du projet.
#
#   verif-version.sh --image <img> [--tag X.Y.Z] [--commit <sha>]   lit /app/version.json dans l'image
#   verif-version.sh --url http://localhost:PORT [--tag X.Y.Z]      GET /__version__ sur l'application
#
# Sortie : « OK » (code 0) ou une ligne par écart, puis « ÉCARTS » (code 1).

set -euo pipefail

IMAGE="" URL="" TAG="" COMMIT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)  IMAGE="${2:?--image attend une image}"; shift ;;
    --url)    URL="${2:?--url attend une URL de base}"; shift ;;
    --tag)    TAG="${2:?--tag attend une version}"; shift ;;
    --commit) COMMIT="${2:?--commit attend un SHA}"; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Option inconnue : $1 (voir --help)" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$IMAGE" || -n "$URL" ]] || { echo "Il faut --image ou --url (voir --help)" >&2; exit 2; }
[[ -n "$IMAGE" && -n "$URL" ]] && { echo "--image et --url sont exclusifs" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq est requis" >&2; exit 2; }

ecarts=0
ecart() { echo "ÉCART : $*"; ecarts=$((ecarts + 1)); }
CONTENU=""

if [[ -n "$IMAGE" ]]; then
  command -v docker >/dev/null || { echo "docker est requis pour --image" >&2; exit 2; }
  # Voie directe : l'image a un `cat`. Repli : image distroless — on extrait le fichier sans l'exécuter.
  if ! CONTENU="$(docker run --rm --entrypoint cat "$IMAGE" /app/version.json 2>/dev/null)"; then
    conteneur="$(docker create "$IMAGE" 2>/dev/null || true)"
    if [[ -n "$conteneur" ]]; then
      tmp="$(mktemp)"
      if docker cp "$conteneur:/app/version.json" "$tmp" 2>/dev/null; then CONTENU="$(cat "$tmp")"; fi
      rm -f "$tmp"; docker rm -f "$conteneur" >/dev/null 2>&1 || true
    fi
  fi
  [[ -n "$CONTENU" ]] || { ecart "/app/version.json absent de l'image $IMAGE"; echo "ÉCARTS ($ecarts)"; exit 1; }
else
  command -v curl >/dev/null || { echo "curl est requis pour --url" >&2; exit 2; }
  entetes="$(mktemp)"
  CONTENU="$(curl -sS -D "$entetes" -o - --max-time 10 "${URL%/}/__version__" || true)"
  statut="$(awk 'toupper($1) ~ /^HTTP\// {code=$2} END {print code}' "$entetes")"
  type_contenu="$(awk 'BEGIN{IGNORECASE=1} /^content-type:/ {print tolower($0)}' "$entetes" | tail -1)"
  rm -f "$entetes"
  [[ "$statut" == "200" ]] || ecart "GET /__version__ répond ${statut:-rien} au lieu de 200"
  [[ "$type_contenu" == *application/json* ]] || ecart "Content-Type « ${type_contenu#content-type: } » au lieu de application/json"
fi

echo "$CONTENU" | jq . >/dev/null 2>&1 || { ecart "le contenu n'est pas du JSON : ${CONTENU:0:120}"; echo "ÉCARTS ($ecarts)"; exit 1; }

# Les six clés : les quatre de Dockerflow, puis les deux extensions de la plateforme.
for cle in source version commit build code_date changes; do
  echo "$CONTENU" | jq -e --arg k "$cle" 'has($k)' >/dev/null || ecart "clé « $cle » absente"
done
echo "$CONTENU" | jq -e '.changes | type == "array"' >/dev/null 2>&1 || ecart "« changes » n'est pas une liste"

version="$(echo "$CONTENU" | jq -r '.version // ""')"
commit="$(echo "$CONTENU" | jq -r '.commit // ""')"
[[ -n "$TAG"    && "$version" != "$TAG"    ]] && ecart "version « $version » ≠ attendue « $TAG »"
[[ -n "$COMMIT" && "$commit"  != "$COMMIT" ]] && ecart "commit « $commit » ≠ attendu « $COMMIT »"
[[ -z "$TAG" && "$version" == "dev" ]] && echo "NOTE : version « dev » — build de poste sans build-args, ou build-args non passés"

if [[ $ecarts -eq 0 ]]; then
  echo "OK : version=$version commit=${commit:-<vide>} changes=$(echo "$CONTENU" | jq '.changes | length') ligne(s)"
  exit 0
fi
echo "ÉCARTS ($ecarts)"
exit 1
