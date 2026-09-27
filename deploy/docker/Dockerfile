FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY db/schema.sql /app/db/schema.sql
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini

# Version : l'image se DÉCLARE elle-même. Passée au build
# (--build-arg DM_IMAGE_TAG=<tag>), elle ne peut plus diverger du tag déployé —
# contrairement à une valeur tenue à la main dans les manifestes, qui est restée
# à 0.9.15 pendant quatre versions sans que rien ne le signale. Un opérateur peut
# toujours l'écraser par la variable d'environnement s'il en a besoin.
ARG DM_IMAGE_TAG=dev
ENV DM_APP_VERSION=${DM_IMAGE_TAG}

# Convention ADR-0004 MirAI next, alignée sur Dockerflow : l'image porte son journal en
# /app/version.json, servi sur /__version__ (lu par le noteur du suivi, dans le cluster).
# Les valeurs viennent de la CI ou des scripts de build (build-args) ; un build de poste
# donne « dev ». VERSION est aliasée sur DM_IMAGE_TAG : les trois chemins de build qui ne
# passent que cet argument produisent quand même la bonne version. Posé avant USER : lisible
# par l'utilisateur non-root.
ARG VERSION=${DM_IMAGE_TAG}
ARG COMMIT=
ARG BUILD=
ARG CODE_DATE=
ARG SOURCE=
COPY scripts/version_json.py CHANGELOG.md* /tmp/version/
RUN python /tmp/version/version_json.py --version "$VERSION" --commit "$COMMIT" --build "$BUILD" \
      --code-date "$CODE_DATE" --source "$SOURCE" --changelog /tmp/version/CHANGELOG.md \
      --out /app/version.json \
 && chmod 0644 /app/version.json \
 && rm -rf /tmp/version

# Defaults
ENV DM_ENROLL_DIR=/data/enroll \
    DM_STORE_ENROLL_LOCALLY=true \
    DM_ALLOW_ORIGINS="*" \
    DM_MAX_BODY_SIZE_MB=10

# Non-root : uid fixe (sans home ni shell) pour satisfaire les PodSecurityContext
# runAsNonRoot/runAsUser des overlays de déploiement. Sans cette directive,
# kubelet refuse de créer le container avec « container has runAsNonRoot and
# image will run as root » (constaté sur llm-proxy en 0.9.0).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin dm-app
USER 10001

EXPOSE 3001

# --proxy-headers : l'app tourne derrière un reverse-proxy (OpenShift Routes /
# nginx) qui termine le TLS ; sans ces flags uvicorn ignore X-Forwarded-Proto/Host
# (forwarded-allow-ips défaut = 127.0.0.1 seulement) et voit du http:// interne,
# ce qui casse les redirections absolues (ex. slash admin). `*` : le pod n'est
# joignable que via le Service/proxy en cluster, on fait confiance aux en-têtes.
# --timeout-graceful-shutdown : laisse les requêtes/jobs en cours se terminer
# avant l'arrêt (SIGTERM K8s).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3001", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
