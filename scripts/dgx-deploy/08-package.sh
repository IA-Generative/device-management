#!/usr/bin/env bash
# scripts/dgx-deploy/08-package.sh
# ---------------------------------------------------------------------------
# Cree une archive tar.gz autonome pour deploiement air-gap sur DGX.
# Contient : manifests rendus, sources kustomize, images Docker, scripts.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
K8S_BASE="$ROOT_DIR/deploy/k8s/base"
K8S_DGX="$ROOT_DIR/deploy/k8s/overlays/dgx"
NAMESPACE="bootstrap"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
step()  { echo -e "\n${GREEN}[STEP]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info()  { echo -e "  → $*"; }

VERSION="${1:-$(date +%Y%m%d-%H%M%S)}"
PKG_NAME="dgx-deploy-${VERSION}"
PKG_DIR="$ROOT_DIR/dist/$PKG_NAME"
PKG_TAR="$ROOT_DIR/dist/${PKG_NAME}.tar.gz"

echo "============================================="
echo " DGX Air-Gap Packaging"
echo " Version: $VERSION"
echo "============================================="

# ── Prereqs ───────────────────────────────────────────────

step "Checking prerequisites"
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found"
ok "kubectl found"

step "Validating kustomize render"
kubectl kustomize "$K8S_DGX" >/dev/null || fail "kustomize render failed — fix before packaging"
ok "kustomize renders OK"

# ── Create package directory ──────────────────────────────

step "Creating package directory"
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR"/{manifests,scripts}

# ── Rendered manifests ────────────────────────────────────

step "Rendering final manifests"
# Render everything, then strip the Secret resource (managed separately by dumb-deploy.sh)
kubectl kustomize "$K8S_DGX" > /tmp/dgx-full.yaml
python3 -c "
import sys
doc = open('/tmp/dgx-full.yaml').read()
parts = doc.split('---\n')
kept = []
for p in parts:
    if 'kind: Secret' in p and 'device-management-secrets' in p:
        continue  # skip the secret
    kept.append(p)
open('$PKG_DIR/manifests/dgx-all.yaml','w').write('---\n'.join(kept))
"
rm -f /tmp/dgx-full.yaml
LINES=$(wc -l < "$PKG_DIR/manifests/dgx-all.yaml" | tr -d ' ')
ok "manifests/dgx-all.yaml ($LINES lines, secret excluded)"

# ── Copy kustomize sources (for on-site tweaks) ──────────

step "Copying kustomize sources"
cp -r "$K8S_BASE" "$PKG_DIR/manifests/base"
cp -r "$K8S_DGX" "$PKG_DIR/manifests/dgx-overlay"
ok "kustomize sources copied"

# ── Copy deploy script + connectivity test ───────────────

step "Copying scripts"
# Main deploy script at root (the only thing the user needs to run)
cp "$SCRIPT_DIR/dumb-deploy.sh" "$PKG_DIR/" || fail "dumb-deploy.sh missing"
chmod +x "$PKG_DIR/dumb-deploy.sh"
ok "dumb-deploy.sh"

# Database schema (loaded automatically by dumb-deploy.sh into postgres)
SCHEMA_SRC="$ROOT_DIR/db/schema.sql"
if [ -f "$SCHEMA_SRC" ]; then
  cp "$SCHEMA_SRC" "$PKG_DIR/schema.sql"
  ok "schema.sql ($(wc -l < "$SCHEMA_SRC" | tr -d ' ') lines)"
else
  warn "schema.sql not found at $SCHEMA_SRC — DB will not be initialized"
fi

# Connectivity test in scripts/
cp "$SCRIPT_DIR/09-connectivity-test.sh" "$PKG_DIR/scripts/"
chmod +x "$PKG_DIR/scripts/09-connectivity-test.sh"
ok "scripts/09-connectivity-test.sh"

# Runbook
cp "$SCRIPT_DIR/RUNBOOK-DGX.md" "$PKG_DIR/" 2>/dev/null && ok "RUNBOOK-DGX.md" || true

# .env.deploy template (the user fills in the DockerHub credentials)
cat > "$PKG_DIR/.env.deploy.example" <<'ENVEOF'
# Copy this file to .env.deploy and fill in the values.
# .env.deploy is sourced automatically by dumb-deploy.sh.
#
# DockerHub credentials for image pull
# All images are mirrored under docker.io/<DOCKERHUB_NAMESPACE>/*
# Get a Personal Access Token from https://hub.docker.com/settings/security
DOCKERHUB_USER=<DOCKERHUB_NAMESPACE>
DOCKERHUB_TOKEN=
ENVEOF
ok ".env.deploy.example"

# Secrets template
if [ -f "$SCRIPT_DIR/.env.secrets.example" ]; then
  cp "$SCRIPT_DIR/.env.secrets.example" "$PKG_DIR/.env.secrets.example"
  ok ".env.secrets.example"
fi

# Non-sensitive config (embedded, combined with secrets at deploy time)
if [ -f "$SCRIPT_DIR/.env.config" ]; then
  cp "$SCRIPT_DIR/.env.config" "$PKG_DIR/.env.config"
  ok ".env.config"
fi

# ── Image list (reference only, images pulled online) ─────

step "Extracting image list from rendered manifests"
IMAGES=$(grep 'image:' "$PKG_DIR/manifests/dgx-all.yaml" | \
  sed 's/.*image: *//' | sed 's/"//g' | sort -u)

echo "  Images required (pulled online by the cluster, amd64):"
echo "$IMAGES" | sed 's/^/    /'
echo "$IMAGES" > "$PKG_DIR/IMAGE_LIST.txt"
ok "IMAGE_LIST.txt"

# dumb-deploy.sh is the only entrypoint, no apply.sh wrapper needed
ok "deploy entrypoint = dumb-deploy.sh"

# ── Generate README ───────────────────────────────────────

step "Generating README"
cat > "$PKG_DIR/README.txt" <<EOF
DGX Deploy Package — ${PKG_NAME}
=================================
Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
From commit: $(cd "$ROOT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")

Contents:
  dumb-deploy.sh               THE deploy script (run this)
  .env.deploy.example          Template for credentials (copy to .env.deploy)
  manifests/dgx-all.yaml       Pre-rendered K8s manifests
  manifests/base/              Kustomize base sources (for on-site tweaks)
  manifests/dgx-overlay/       Kustomize DGX overlay sources
  scripts/09-connectivity-test.sh   Connectivity test (Job-based, no exec needed)
  IMAGE_LIST.txt               List of required images
  RUNBOOK-DGX.md               Step-by-step reference

Quick start (3 commands) :
  1. tar xzf ${PKG_NAME}.tar.gz && cd ${PKG_NAME}
  2. cp .env.deploy.example .env.deploy && \$EDITOR .env.deploy   # paste SCW key
  3. ./dumb-deploy.sh

Or fully non-interactive :
  SCW_SECRET_KEY=xxx-xxx-xxx ./dumb-deploy.sh

What dumb-deploy.sh does :
  - Creates the bootstrap namespace
  - Creates/updates the regcred secret (Scaleway registry credentials)
  - Applies manifests/dgx-all.yaml
  - Waits for all deployments to be ready
  - Shows final pod status

Connectivity check (after deploy) :
  bash scripts/09-connectivity-test.sh

Manual / fallback :
  kubectl apply -f manifests/dgx-all.yaml
EOF
ok "README.txt"

# ── Create tarball ────────────────────────────────────────

step "Creating archive"
mkdir -p "$ROOT_DIR/dist"
(cd "$ROOT_DIR/dist" && tar czf "${PKG_NAME}.tar.gz" "$PKG_NAME")
TOTAL_SIZE=$(du -h "$PKG_TAR" | cut -f1)

# ── Cleanup uncompressed dir ─────────────────────────────
rm -rf "$PKG_DIR"

echo ""
echo "============================================="
echo -e "${GREEN} PACKAGING COMPLETE${NC}"
echo ""
echo "  Archive : dist/${PKG_NAME}.tar.gz"
echo "  Size    : $TOTAL_SIZE"
echo ""
echo "  Transfer to DGX, extract, and run:"
echo "    scp dist/${PKG_NAME}.tar.gz user@dgx:/tmp/"
echo "    ssh user@dgx"
echo "    tar xzf /tmp/${PKG_NAME}.tar.gz"
echo "    cd ${PKG_NAME}"
echo "    cp .env.deploy.example .env.deploy && \$EDITOR .env.deploy"
echo "    ./dumb-deploy.sh"
echo "============================================="
