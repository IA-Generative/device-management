# Deprecated

`infra-minimal` is deprecated.

Use the new profile-based deployment:

- `deploy/k8s/base`
- `deploy/k8s/overlays/local`
- `deploy/k8s/overlays/scaleway`
- `deploy/k8s/overlays/dgx`

Unified commands:

```bash
./scripts/k8s/render.sh <local|scaleway|dgx>
./scripts/k8s/deploy.sh <local|scaleway|dgx>
./scripts/k8s/validate.sh <local|scaleway|dgx>
```
