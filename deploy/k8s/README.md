# Kubernetes Profiles (Agnostic Deployment)

This directory unifies Kubernetes deployment with one base and three overlays:

- `base`: shared manifests (device-management, relay-assistant, telemetry-relay, postgres, adminer, filebrowser)
- `overlays/local`: local Kubernetes profile (`http://bootstrap.home`)
- `overlays/scaleway`: Scaleway profile (`https://bootstrap.fake-domain.name`)
- `overlays/dgx`: DGX profile (`https://onyxia.gpu.minint.fr/bootstrap`)

## Commands

```bash
cp .env.registry.example .env.registry
./scripts/k8s/create-registry-secret.sh local
./scripts/k8s/render.sh local
./scripts/k8s/deploy.sh local
./scripts/k8s/validate.sh local
```

Replace `local` with `scaleway` or `dgx`.

`regcred` is required by deployments (`imagePullSecrets`). Keep registry credentials out of Git and create/update the secret from `.env.registry`.

## HTTP/HTTPS

- `local` uses `http://` for development only.
- `scaleway` and `dgx` use `https://` and require valid certificates trusted by clients.
- For production, keep TLS validation enabled and never bypass certificate checks.

## Migration from infra-minimal

1. Validate profile overlays (`local`, `scaleway`, `dgx`) with `scripts/k8s/validate.sh`.
2. Switch CI/CD and ops scripts to `scripts/k8s/*`.
3. Freeze `infra-minimal` (no new changes).
4. Remove `infra-minimal` once parity is confirmed.
