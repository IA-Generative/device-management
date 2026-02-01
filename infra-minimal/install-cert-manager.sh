#!/usr/bin/env sh
set -eu

# Install cert-manager and create Let's Encrypt ClusterIssuers (staging + prod).
# Usage: ./install-cert-manager.sh you@example.com

EMAIL="${1:-}"
if [ -z "$EMAIL" ]; then
  echo "Usage: $0 <email>" >&2
  exit 1
fi

# Namespace
kubectl get ns cert-manager >/dev/null 2>&1 || kubectl create ns cert-manager

# Add repo & install cert-manager
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
helm repo update

helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --version v1.15.3 \
  --set installCRDs=true

# Wait for cert-manager to be ready
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=120s
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=120s
kubectl -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=120s

# ClusterIssuers (staging + prod)
cat <<EOT | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-staging
spec:
  acme:
    email: ${EMAIL}
    server: https://acme-staging-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-staging
    solvers:
    - http01:
        ingress:
          class: nginx
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    email: ${EMAIL}
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
    - http01:
        ingress:
          class: nginx
EOT

echo "cert-manager installé. ClusterIssuers créés:"
kubectl get clusterissuer | grep letsencrypt || true
