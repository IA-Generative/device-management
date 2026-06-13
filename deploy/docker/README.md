# Déploiement local Docker (dev)

Lancement local de device-management via Docker Compose. **Build depuis les sources**
de ce dépôt (`context: ../..`) → image construite pour l'**architecture de l'hôte**
(donc **arm64** sur un Mac Apple Silicon, sans émulation).

> ⚠️ Arch : ce compose est pour le **dev local** (arm64 ici). Les **clusters k8s
> sont amd64** et tirent l'image publiée `docker.io/etiquet/device-management:<tag>`
> (construite séparément avec `docker buildx --platform linux/amd64 --push`). Ne pas
> mélanger : le déploiement k8s vit dans le dépôt **device-management-private**.

## Démarrage

```bash
cd deploy/docker
cp .env.example .env                  # config non sensible (éditer si besoin)
cp .env.secrets.example .env.secrets  # secrets locaux — gitignoré, NE PAS committer
$EDITOR .env.secrets                  # renseigner les valeurs (ou laisser vide en dev)
docker compose up --build
```

API sur http://localhost:3001 (par défaut `DM_PORT=3001`).

## Fichiers
- `docker-compose.yml` — services (device-management, queue-worker, relay-assistant, …).
- `.env.example` / `.env.secrets.example` — gabarits **versionnés** (sans secret).
- `.env` / `.env.secrets` — valeurs réelles, **gitignorés** (jamais committés).
- `Dockerfile` — image de l'app (utilisée aussi pour le build amd64 des clusters).

## Postgres local
Par défaut le compose attend un postgres. Pour un postgres conteneurisé :
décommenter le service `postgres` du compose et mettre `DM_POSTGRES_LOCAL=true`
dans `.env` (voir commentaires du `docker-compose.yml`).
