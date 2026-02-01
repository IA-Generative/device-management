#!/usr/bin/env sh
set -eu

NAMESPACE="${NAMESPACE:-bootstrap}"
REGISTRY="rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi"

if [ -z "${SCW_SECRET_KEY:-}" ]; then
  echo "SCW_SECRET_KEY is required to create the registry secret" >&2
  exit 1
fi

kubectl -n "$NAMESPACE" delete secret scw-regcred --ignore-not-found
kubectl -n "$NAMESPACE" create secret docker-registry scw-regcred \
  --docker-server="$REGISTRY" \
  --docker-username=nologin \
  --docker-password="$SCW_SECRET_KEY"

echo "Created scw-regcred in namespace $NAMESPACE for $REGISTRY"
