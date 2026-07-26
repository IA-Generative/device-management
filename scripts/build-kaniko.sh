#!/usr/bin/env bash
# Build in-cluster de l'image device-management avec Kaniko (sans démon Docker).
#
# Alternative à ./scripts/build-k8s.sh (docker buildx) quand on ne veut pas —
# ou ne peut pas — construire depuis un poste : pas de Docker local, poste sur
# un lien lent (le push part du cluster, pas de chez vous), ou build à lancer
# depuis un runner CI qui n'a qu'un kubeconfig.
#
#   ./scripts/build-kaniko.sh                    # tag = VERSION, branche courante
#   ./scripts/build-kaniko.sh 0.9.15             # tag explicite
#   ./scripts/build-kaniko.sh 0.9.15 --cache     # réutilise le cache de couches
#   ./scripts/build-kaniko.sh --dry-run          # affiche le Job, n'applique rien
#
# Options :
#   -n, --namespace <ns>     namespace du Job     (défaut: $KANIKO_NAMESPACE / REGISTRY_NAMESPACE / bootstrap)
#   -c, --kube-context <ctx> contexte kubectl     (défaut: contexte courant, jamais modifié globalement)
#       --ref <git-ref>      réf à construire     (défaut: branche courante)
#       --push-secret <nom>  secret de push       (défaut: $KANIKO_PUSH_SECRET / REGISTRY_SECRET_NAME / regcred)
#       --cache              active le cache Kaniko (dépôt <image>-cache, push requis)
#       --no-latest          ne pousse pas le tag `latest`
#       --allow-unpushed     désactive la garde « commit local présent sur origin »
#       --keep               conserve le Job après succès (debug)
#       --dry-run            rend le manifeste sur stdout sans l'appliquer
#
# ATTENTION — Kaniko construit depuis GIT, pas depuis votre copie de travail :
# ce qui est construit est ce qui est POUSSÉ sur origin. La garde ci-dessous
# refuse de lancer si le HEAD local n'est pas sur origin (contournable avec
# --allow-unpushed, par exemple pour construire une réf ancienne).
#
# Limite assumée : image MONO-ARCH (arch des nœuds). Pour un manifeste
# multi-arch, utiliser build-k8s.sh. Voir deploy/k8s/kaniko/README.md.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/deploy/k8s/kaniko/kaniko-build-job.yaml"
KANIKO_IMAGE="${KANIKO_IMAGE:-gcr.io/kaniko-project/executor:v1.23.2}"
DOCKERFILE="deploy/docker/Dockerfile"

TAG=""; NAMESPACE=""; KUBE_CONTEXT=""; GIT_REF=""; PUSH_SECRET=""
USE_CACHE=0; PUSH_LATEST=1; ALLOW_UNPUSHED=0; KEEP=0; DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--namespace)     NAMESPACE="$2"; shift 2 ;;
    -c|--kube-context)  KUBE_CONTEXT="$2"; shift 2 ;;
    --ref)              GIT_REF="$2"; shift 2 ;;
    --push-secret)      PUSH_SECRET="$2"; shift 2 ;;
    --cache)            USE_CACHE=1; shift ;;
    --no-latest)        PUSH_LATEST=0; shift ;;
    --allow-unpushed)   ALLOW_UNPUSHED=1; shift ;;
    --keep)             KEEP=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    -h|--help)          sed -n '2,32p' "$0"; exit 0 ;;
    -*)                 echo "ERROR: option inconnue: $1" >&2; exit 2 ;;
    *)                  [ -z "$TAG" ] && TAG="$1" || { echo "ERROR: tag déjà fourni ($TAG)" >&2; exit 2; }; shift ;;
  esac
done

# ---- Registre : même convention que build-k8s.sh (.env.registry gitignoré)
ENV_REGISTRY="$ROOT_DIR/.env.registry"
if [ ! -f "$ENV_REGISTRY" ]; then
  echo "ERROR: $ENV_REGISTRY introuvable. Copiez .env.registry.example et renseignez-le." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_REGISTRY"

# DM_REGISTRY_OVERRIDE : cible ponctuelle différente du REGISTRY_SERVER
# (ex. l'intégration tire de docker.io/<ns> alors que .env.registry pointe
# le registre Scaleway) — sans éditer le fichier de creds.
REGISTRY="${DM_REGISTRY_OVERRIDE:-${REGISTRY_SERVER:?REGISTRY_SERVER absent de .env.registry}}"
IMAGE="$REGISTRY/device-management"
NAMESPACE="${NAMESPACE:-${KANIKO_NAMESPACE:-${REGISTRY_NAMESPACE:-bootstrap}}}"
PUSH_SECRET="${PUSH_SECRET:-${KANIKO_PUSH_SECRET:-${REGISTRY_SECRET_NAME:-regcred}}}"

if [ -z "$TAG" ]; then
  TAG="$( { [ -f "$ROOT_DIR/VERSION" ] && head -1 "$ROOT_DIR/VERSION" | tr -d '[:space:]'; } || true )"
  [ -n "$TAG" ] || { echo "ERROR: aucun tag fourni et VERSION vide/absente." >&2; exit 1; }
  # stderr : stdout doit rester le manifeste seul, pour que --dry-run soit pipeable.
  echo "Tag par défaut (VERSION): $TAG" >&2
fi

# ---- Réf git à construire + garde « c'est bien ce commit qui sera construit »
GIT_URL="$(git -C "$ROOT_DIR" remote get-url origin)"
case "$GIT_URL" in *.git) ;; *) GIT_URL="$GIT_URL.git" ;; esac
BRANCH="${GIT_REF:-$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)}"
LOCAL_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
REMOTE_SHA="$(git -C "$ROOT_DIR" ls-remote origin "$BRANCH" 2>/dev/null | awk '{print $1}' | head -1)"

if [ "$ALLOW_UNPUSHED" -eq 0 ]; then
  if [ -z "$REMOTE_SHA" ]; then
    echo "ERROR: '$BRANCH' n'existe pas sur origin — Kaniko construit depuis git." >&2
    echo "       Poussez la branche, ou relancez avec --allow-unpushed." >&2
    exit 1
  fi
  if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    echo "ERROR: le HEAD local ($(git -C "$ROOT_DIR" rev-parse --short HEAD)) diffère de" >&2
    echo "       origin/$BRANCH (${REMOTE_SHA:0:7}). Kaniko construirait le commit DISTANT." >&2
    echo "       Poussez d'abord, ou relancez avec --allow-unpushed." >&2
    exit 1
  fi
  if [ -n "$(git -C "$ROOT_DIR" status --porcelain -- ':!docs' 2>/dev/null)" ]; then
    echo "WARN: copie de travail modifiée — ces changements ne seront PAS dans l'image." >&2
  fi
fi
BUILD_SHA="${REMOTE_SHA:-$LOCAL_SHA}"

# Contexte git de Kaniko. https → dépôt public, aucun credential nécessaire.
GIT_CONTEXT="git://${GIT_URL#https://}#refs/heads/$BRANCH"

# ---- Arguments optionnels
EXTRA_ARGS=()
[ "$PUSH_LATEST" -eq 1 ] && EXTRA_ARGS+=("--destination=$IMAGE:latest")
if [ "$USE_CACHE" -eq 1 ]; then
  EXTRA_ARGS+=("--cache=true" "--cache-repo=$IMAGE-cache")
fi

JOB_NAME="dm-kaniko-${TAG//[^a-zA-Z0-9]/-}-${BUILD_SHA:0:7}"
JOB_NAME="$(echo "$JOB_NAME" | tr '[:upper:]' '[:lower:]' | cut -c1-63)"

MANIFEST="$(
  ROOT_DIR="$ROOT_DIR" TEMPLATE="$TEMPLATE" JOB_NAME="$JOB_NAME" NAMESPACE="$NAMESPACE" \
  KANIKO_IMAGE="$KANIKO_IMAGE" GIT_CONTEXT="$GIT_CONTEXT" DOCKERFILE="$DOCKERFILE" \
  IMAGE="$IMAGE" TAG="$TAG" GIT_SHA="$BUILD_SHA" PUSH_SECRET="$PUSH_SECRET" \
  EXTRA_ARGS="$(printf '%s\n' "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")" \
  python3 - <<'PY'
import os
tpl = open(os.environ["TEMPLATE"], encoding="utf-8").read()
extra = [a for a in os.environ.get("EXTRA_ARGS", "").splitlines() if a.strip()]
tokens = {
    "__JOB_NAME__": os.environ["JOB_NAME"],
    "__NAMESPACE__": os.environ["NAMESPACE"],
    "__KANIKO_IMAGE__": os.environ["KANIKO_IMAGE"],
    "__GIT_CONTEXT__": os.environ["GIT_CONTEXT"],
    "__DOCKERFILE__": os.environ["DOCKERFILE"],
    "__IMAGE__": os.environ["IMAGE"],
    "__TAG__": os.environ["TAG"],
    "__GIT_SHA__": os.environ["GIT_SHA"],
    "__PUSH_SECRET__": os.environ["PUSH_SECRET"],
}
for k, v in tokens.items():
    tpl = tpl.replace(k, v)

# La ligne porteuse de __EXTRA_ARGS__ est remplacée par 0..n entrées, à son
# indentation propre : le gabarit reste ainsi du YAML valide et relisible.
out = []
for line in tpl.splitlines():
    if "__EXTRA_ARGS__" in line:
        indent = " " * (len(line) - len(line.lstrip()))
        out.extend(f'{indent}- "{a}"' for a in extra)
    else:
        out.append(line)
print("\n".join(out))
PY
)"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "$MANIFEST"
  exit 0
fi

KCTL=(kubectl)
[ -n "$KUBE_CONTEXT" ] && KCTL+=(--context "$KUBE_CONTEXT")

echo "== Build in-cluster (Kaniko) =="
echo "   cluster   : $("${KCTL[@]}" config current-context)"
echo "   namespace : $NAMESPACE"
echo "   source    : $GIT_CONTEXT @ ${BUILD_SHA:0:7}"
echo "   image     : $IMAGE:$TAG$([ "$PUSH_LATEST" -eq 1 ] && echo " (+ :latest)")"
echo "   secret    : $PUSH_SECRET"

if ! "${KCTL[@]}" get secret "$PUSH_SECRET" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "ERROR: secret '$PUSH_SECRET' absent du namespace '$NAMESPACE'." >&2
  echo "       Créez-le : ./scripts/k8s/create-registry-secret.sh <env>" >&2
  exit 1
fi

"${KCTL[@]}" delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null
echo "$MANIFEST" | "${KCTL[@]}" apply -f - >/dev/null
echo "   job       : $JOB_NAME"

echo "-- logs --"
for _ in $(seq 1 60); do
  POD="$("${KCTL[@]}" get pods -n "$NAMESPACE" -l job-name="$JOB_NAME" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$POD" ] && break
  sleep 2
done
[ -n "${POD:-}" ] || { echo "ERROR: aucun pod pour le job $JOB_NAME" >&2; exit 1; }
"${KCTL[@]}" logs -f "$POD" -n "$NAMESPACE" 2>&1 || true

if "${KCTL[@]}" wait --for=condition=complete --timeout=30m \
     job/"$JOB_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "== OK : $IMAGE:$TAG poussé depuis le cluster (commit ${BUILD_SHA:0:7}) =="
  [ "$KEEP" -eq 1 ] || "${KCTL[@]}" delete job "$JOB_NAME" -n "$NAMESPACE" >/dev/null
  exit 0
fi

echo "== ÉCHEC du build. Job conservé pour diagnostic : $JOB_NAME ==" >&2
"${KCTL[@]}" describe job "$JOB_NAME" -n "$NAMESPACE" | tail -20 >&2
exit 1
