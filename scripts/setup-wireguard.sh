#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# setup-wireguard.sh — WireGuard secret provisioning for k8s
#
# Usage:
#   ./scripts/setup-wireguard.sh                  # interactive mode
#   ./scripts/setup-wireguard.sh /path/to/wg0.conf  # import existing conf
#
# Creates the k8s Secret "wireguard-config" in namespace "bootstrap".
# Keys never leave your machine.
#
# Prerequisites: wg (wireguard-tools) installed locally.
#   brew install wireguard-tools   (macOS)
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

NS="bootstrap"
SECRET_NAME="wireguard-config"
CONTEXT="${KUBECONTEXT:-$(kubectl config current-context)}"

echo "=== WireGuard Secret Setup (namespace: $NS, context: $CONTEXT) ==="
echo ""

if [ "${1:-}" != "" ] && [ -f "$1" ]; then
  # ── Import mode: use existing wg conf file ──────────────────────
  WG_CONF_FILE="$1"
  echo "Importing config from: $WG_CONF_FILE"
  echo ""
  echo "── Preview (keys redacted) ──"
  sed 's/PrivateKey = .*/PrivateKey = [REDACTED]/' "$WG_CONF_FILE" \
    | sed 's/PresharedKey = .*/PresharedKey = [REDACTED]/'
  echo ""

  read -rp "Apply this secret to cluster? [y/N] " CONFIRM
  if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi

  kubectl --context "$CONTEXT" -n "$NS" delete secret "$SECRET_NAME" --ignore-not-found
  kubectl --context "$CONTEXT" -n "$NS" create secret generic "$SECRET_NAME" \
    --from-file=wg0.conf="$WG_CONF_FILE"

  echo ""
  echo "✓ Secret '$SECRET_NAME' created in namespace '$NS'."
  echo "  Deploy the WireGuard pod:  kubectl apply -k deploy/k8s/overlays/scaleway"
  exit 0
fi

# ── Interactive mode: generate keys + build conf ──────────────────
if ! command -v wg &>/dev/null; then
  echo "ERROR: 'wg' not found. Install wireguard-tools:"
  echo "  brew install wireguard-tools   (macOS)"
  echo "  apt install wireguard-tools    (Debian/Ubuntu)"
  exit 1
fi

CLIENT_PRIVKEY=$(wg genkey)
CLIENT_PUBKEY=$(echo "$CLIENT_PRIVKEY" | wg pubkey)

echo "Client public key (give this to your WireGuard server admin):"
echo ""
echo "  $CLIENT_PUBKEY"
echo ""

read -rp "WireGuard server endpoint (host:port, e.g. vpn.example.com:51820): " WG_ENDPOINT
read -rp "Server public key: " SERVER_PUBKEY
read -rp "Client tunnel IP (e.g. 10.0.0.2/32): " CLIENT_ADDRESS
read -rp "Pre-shared key (leave empty if none): " PSK

# Resolve SSO host for AllowedIPs
SSO_HOST="sso.mirai.interieur.gouv.fr"
SSO_IP=$(dig +short "$SSO_HOST" | head -1)
if [ -z "$SSO_IP" ]; then
  echo "WARNING: could not resolve $SSO_HOST, using 0.0.0.0/0 as AllowedIPs"
  ALLOWED_IPS="0.0.0.0/0"
else
  echo "Resolved $SSO_HOST → $SSO_IP"
  ALLOWED_IPS="$SSO_IP/32"
fi

PSK_LINE=""
if [ -n "$PSK" ]; then
  PSK_LINE="PresharedKey = $PSK"
fi

WG_CONF="[Interface]
PrivateKey = $CLIENT_PRIVKEY
Address = $CLIENT_ADDRESS

[Peer]
PublicKey = $SERVER_PUBKEY
${PSK_LINE}
Endpoint = $WG_ENDPOINT
AllowedIPs = $ALLOWED_IPS
PersistentKeepalive = 25
"

echo ""
echo "── Generated wg0.conf (preview, keys redacted) ──"
echo "$WG_CONF" | sed 's/PrivateKey = .*/PrivateKey = [REDACTED]/' | sed 's/PresharedKey = .*/PresharedKey = [REDACTED]/'
echo ""

read -rp "Apply this secret to cluster? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

kubectl --context "$CONTEXT" -n "$NS" delete secret "$SECRET_NAME" --ignore-not-found
kubectl --context "$CONTEXT" -n "$NS" create secret generic "$SECRET_NAME" \
  --from-literal=wg0.conf="$WG_CONF"

echo ""
echo "✓ Secret '$SECRET_NAME' created in namespace '$NS'."
echo ""
echo "Next steps:"
echo "  1. Give your server admin the client public key above"
echo "  2. Ask them to add a [Peer] block for this key with AllowedIPs = $CLIENT_ADDRESS"
echo "  3. Deploy the WireGuard pod:  kubectl apply -k deploy/k8s/overlays/scaleway"
