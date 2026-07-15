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
