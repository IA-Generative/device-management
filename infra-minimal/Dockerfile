FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY config /app/config
COPY infra-minimal/db-schema.sql /app/infra-minimal/db-schema.sql

# Defaults
ENV DM_ENROLL_DIR=/data/enroll \
    DM_STORE_ENROLL_LOCALLY=true \
    DM_ALLOW_ORIGINS="*" \
    DM_MAX_BODY_SIZE_MB=10

EXPOSE 3001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3001"]
