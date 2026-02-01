#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="ingress-nginx"
RELEASE="ingress-nginx"

helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm upgrade --install "${RELEASE}" ingress-nginx/ingress-nginx \
  --namespace "${NAMESPACE}" --create-namespace \
  --set controller.ingressClassResource.default=true

kubectl -n "${NAMESPACE}" rollout status deploy/"${RELEASE}"-controller
kubectl get ingressclass
kubectl -n "${NAMESPACE}" get svc