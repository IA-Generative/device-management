from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

import uvicorn

try:
    import psycopg2  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    psycopg2 = None  # type: ignore

if os.getenv("RELOAD", "").lower() == "true" and "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"

from .settings import settings
from .s3 import s3_client

app = FastAPI(title="Device Management API", version="0.1.0")
logger = logging.getLogger("device-management")

# ---- CORS
origins = [o.strip() for o in settings.allow_origins.split(",")] if settings.allow_origins else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

MAX_BODY_BYTES = settings.max_body_size_mb * 1024 * 1024
S3_BINARIES_PREFIX = settings.s3_prefix_binaries


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# Supports env-var placeholders in config templates.
# Preferred syntax: ${{VARNAME}}
# Backward-compatible syntax: ${VARNAME}
_TEMPLATE_VAR_RE = re.compile(r"\$\{\{([A-Z0-9_]+)\}\}|\$\{([A-Z0-9_]+)\}")


def _repo_root() -> str:
    # app/ is a package folder; repo root is one level above
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


SCHEMA_SQL_PATH = os.path.join(_repo_root(), "infra-minimal", "db-schema.sql")


def _db_url() -> str | None:
    return os.getenv("DATABASE_URL") or settings.database_url or None


def _db_url_bootstrap() -> str | None:
    base = _db_url()
    if not base:
        return None
    return _with_db(base, "bootstrap")


def _with_db(url: str, db_name: str) -> str:
    parsed = urlparse(url)
    path = f"/{db_name}"
    return urlunparse(parsed._replace(path=path))


def _admin_db_url(base_url: str) -> str | None:
    explicit = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DM_DATABASE_ADMIN_URL")
    if explicit:
        return explicit

    parsed = urlparse(base_url)
    admin_user = (
        os.getenv("DB_ADMIN_USER")
        or os.getenv("POSTGRES_ADMIN_USER")
        or os.getenv("POSTGRES_USER")
        or "postgres"
    )
    admin_password = (
        os.getenv("DB_ADMIN_PASSWORD")
        or os.getenv("POSTGRES_ADMIN_PASSWORD")
        or os.getenv("POSTGRES_PASSWORD")
    )

    if admin_password:
        netloc = f"{admin_user}:{admin_password}@{parsed.hostname}"
    else:
        netloc = f"{admin_user}@{parsed.hostname}"

    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse(parsed._replace(netloc=netloc))


def _ensure_database_exists(db_url: str, db_name: str = "bootstrap") -> None:
    if psycopg2 is None:
        raise RuntimeError(
            "psycopg2 is not installed. Install it with: pip install psycopg2-binary (dev) or psycopg2 (prod)."
        )
    admin_url = _with_db(db_url, "postgres")
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()


def _ensure_dev_role(admin_url: str) -> None:
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'dev'")
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute("CREATE ROLE dev LOGIN PASSWORD 'dev'")
            # Enforce least privilege for dev role.
            try:
                cur.execute(
                    "ALTER ROLE dev NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            except psycopg2.Error:
                logger.warning("Skipping ALTER ROLE dev (insufficient privilege)")
    finally:
        conn.close()


def _ensure_dev_privileges(admin_bootstrap_url: str) -> None:
    conn = psycopg2.connect(admin_bootstrap_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("GRANT CONNECT ON DATABASE bootstrap TO dev")
            cur.execute("GRANT USAGE ON SCHEMA public TO dev")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dev")
            cur.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dev"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO dev"
            )
    finally:
        conn.close()

def _apply_schema(db_url: str) -> None:
    if psycopg2 is None:
        raise RuntimeError(
            "psycopg2 is not installed. Install it with: pip install psycopg2-binary (dev) or psycopg2 (prod)."
        )
    if not os.path.isfile(SCHEMA_SQL_PATH):
        raise FileNotFoundError(f"Schema SQL not found: {SCHEMA_SQL_PATH}")
    with open(SCHEMA_SQL_PATH, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def _wait_for_db(db_url: str, timeout_seconds: int = 30, interval_seconds: float = 1.0) -> None:
    if psycopg2 is None:
        raise RuntimeError(
            "psycopg2 is not installed. Install it with: pip install psycopg2-binary (dev) or psycopg2 (prod)."
        )
    deadline = time.time() + timeout_seconds
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(db_url)
            conn.close()
            return
        except psycopg2.OperationalError as exc:
            last_exc = exc
            time.sleep(interval_seconds)
    if last_exc:
        raise last_exc


def _extract_identity(request: Request, body_obj: dict | None = None) -> tuple[str, str, str]:
    email = request.headers.get("X-User-Email") or (body_obj or {}).get("email") or "unknown@local"
    client_uuid = request.headers.get("X-Client-UUID") or (body_obj or {}).get("client_uuid") or "00000000-0000-0000-0000-000000000000"
    fingerprint = request.headers.get("X-Encryption-Key-Fingerprint") or (body_obj or {}).get("encryption_key_fingerprint") or "unknown"
    return email, client_uuid, fingerprint


def _log_device_connection(
    *,
    action: str,
    email: str,
    client_uuid: str,
    encryption_key_fingerprint: str,
    source_ip: str | None,
    user_agent: str | None,
) -> None:
    if action == "HEALTHZ":
        return
    if psycopg2 is None:
        # DB logging is optional; if psycopg2 isn't installed, just skip.
        return
    db_url = _db_url_bootstrap()
    if not db_url:
        return
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_connections (
                    email, client_uuid, action, encryption_key_fingerprint,
                    connected_at, source_ip, user_agent
                ) VALUES (%s, %s, %s, %s, now(), %s, %s)
                """,
                (email, client_uuid, action, encryption_key_fingerprint, source_ip, user_agent),
            )
    finally:
        conn.close()


def _load_config_template(profile: str) -> dict:
    """Load a config template JSON from `config/`.

    Resolution order:
    - config/config.<profile>.json
    - config/config.json
    """
    base = os.path.join(_repo_root(), "config")
    candidates = [
        os.path.join(base, f"config.{profile}.json"),
        os.path.join(base, "config.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError("No config template found in ./config (expected config.json)")


def _substitute_env_in_str(value: str) -> str:
    """Replace ${{VARNAME}} (or legacy ${VARNAME}) with os.environ['VARNAME'] if set, else empty string."""

    def repl(m: re.Match[str]) -> str:
        # group(1) matches the preferred ${{VARNAME}} syntax,
        # group(2) matches the legacy ${VARNAME} syntax.
        var = m.group(1) or m.group(2)
        return os.getenv(var or "", "")

    return _TEMPLATE_VAR_RE.sub(repl, value)


def _substitute_env(obj):
    """Recursively substitute env vars in any string values."""
    if isinstance(obj, dict):
        return {k: _substitute_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(v) for v in obj]
    if isinstance(obj, str):
        return _substitute_env_in_str(obj)
    return obj


def _apply_overrides(cfg: dict) -> dict:
    """Apply targeted overrides from env (e.g., telemetrySel)."""
    # Keep existing structure but allow overriding config.telemetrySel from env.
    telemetry_salt = os.getenv("DM_TELEMETRY_SALT") or os.getenv("TELEMETRY_SALT")
    if telemetry_salt:
        cfg.setdefault("config", {})
        if isinstance(cfg["config"], dict):
            cfg["config"]["telemetrySel"] = telemetry_salt
    return cfg


@app.get("/healthz")
def healthz():
    errors: list[str] = []
    checks: dict[str, dict[str, str]] = {}

    if settings.store_enroll_locally:
        try:
            _ensure_dir(settings.enroll_dir)
            test_path = os.path.join(settings.enroll_dir, ".write_test")
            with open(test_path, "wb") as f:
                f.write(b"ok")
            os.remove(test_path)
            checks["local_storage"] = {"status": "ok"}
        except Exception as e:
            errors.append(f"Local enroll_dir not writable: {e!r}")
            checks["local_storage"] = {"status": "error", "detail": str(e)}
    else:
        checks["local_storage"] = {"status": "skipped"}

    s3_required = settings.store_enroll_s3 or settings.binaries_mode in ("presign", "proxy")
    if s3_required and not settings.s3_bucket:
        errors.append("S3 bucket is not configured (DM_S3_BUCKET missing).")
        checks["s3"] = {"status": "error", "detail": "bucket missing"}
    elif settings.s3_bucket:
        try:
            s3 = s3_client()
            s3.head_bucket(Bucket=settings.s3_bucket)
            checks["s3"] = {"status": "ok"}
        except Exception as e:
            errors.append(f"S3 not reachable or unauthorized: {e!r}")
            checks["s3"] = {"status": "error", "detail": str(e)}
    else:
        checks["s3"] = {"status": "skipped"}

    db_url = _db_url_bootstrap() or _db_url()
    if not db_url:
        errors.append("Database URL is not configured.")
        checks["db"] = {"status": "error", "detail": "DATABASE_URL missing"}
    elif psycopg2 is None:
        errors.append("psycopg2 is not installed; cannot verify DB connection.")
        checks["db"] = {"status": "error", "detail": "psycopg2 missing"}
    else:
        try:
            conn = psycopg2.connect(db_url, connect_timeout=3)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                checks["db"] = {"status": "ok"}
            finally:
                conn.close()
        except Exception as e:
            errors.append(f"DB not reachable or unauthorized: {e!r}")
            checks["db"] = {"status": "error", "detail": str(e)}

    if errors:
        return JSONResponse(
            status_code=200,
            media_type="application/problem+json",
            content={
                "type": "https://example.com/problems/dependency-check",
                "title": "Dependency check failed",
                "status": 200,
                "detail": "One or more dependencies are not healthy.",
                "checks": checks,
                "errors": errors,
            },
        )

    return JSONResponse(
        status_code=200,
        media_type="application/problem+json",
        content={
            "type": "https://example.com/problems/dependency-check",
            "title": "OK",
            "status": 200,
            "detail": "All dependencies are healthy.",
            "checks": checks,
        },
    )



@app.get("/config/config.json")
def get_config(profile: str | None = None):
    """Return remote-config JSON.

    The response is loaded from a static template file under `exemple/` and supports
    placeholder substitution with environment variables using the syntax: ${{VARNAME}}.

    Profile selection:
    - Request: /config/config.json?profile=dev|prod
    - Default: DM_CONFIG_PROFILE (defaults to "prod")
    """
    prof = (profile or os.getenv("DM_CONFIG_PROFILE", "prod")).strip().lower()
    if prof not in ("dev", "prod", "int", "llama", "gptoss"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "profile must be 'dev' or 'prod' or 'int' "})

    try:
        cfg = _load_config_template(prof)
    except FileNotFoundError as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    # 1) generic substitution ${VARNAME}
    cfg = _substitute_env(cfg)

    # 2) targeted overrides (e.g. telemetrySel)
    cfg = _apply_overrides(cfg)

    # 3) keep top-level enable switch from service settings if you still want a global kill-switch
    cfg["enabled"] = bool(settings.config_enabled)

    try:
        _log_device_connection(
            action="CONFIG_GET",
            email="system@local",
            client_uuid="00000000-0000-0000-0000-000000000000",
            encryption_key_fingerprint="none",
            source_ip=None,
            user_agent=None,
        )
    except Exception:
        logger.exception("Failed to log config call")

    return JSONResponse(content=cfg, headers={"Cache-Control": "no-store"})


@app.api_route("/enroll", methods=["POST", "PUT", "OPTIONS"])
async def enroll(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204)

    body = await request.body()
    if len(body) == 0:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Empty body"})
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"ok": False, "error": "Body too large"})

    try:
        body_obj = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Body is not valid JSON"})

    epoch_ms = int(time.time() * 1000)
    rid = uuid.uuid4().hex
    fname = f"{epoch_ms}-{rid}.json"

    stored = {}

    if settings.store_enroll_locally:
        _ensure_dir(settings.enroll_dir)
        path = os.path.join(settings.enroll_dir, fname)
        try:
            with open(path, "wb") as f:
                f.write(body)
            stored["local"] = path
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot write local file: {e!r}")

    if settings.store_enroll_s3:
        if not settings.s3_bucket:
            raise HTTPException(status_code=500, detail="S3 bucket not configured (DM_S3_BUCKET).")
        key = f"{settings.s3_prefix_enroll.rstrip('/')}/{fname}"
        try:
            s3 = s3_client()
            s3.put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            stored["s3"] = f"s3://{settings.s3_bucket}/{key}"
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot write to S3: {e!r}")

    try:
        email, client_uuid, fingerprint = _extract_identity(request, body_obj=body_obj)
        _log_device_connection(
            action="ENROLL",
            email=email,
            client_uuid=client_uuid,
            encryption_key_fingerprint=fingerprint,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception("Failed to log enroll call")

    return JSONResponse(status_code=201, content={"ok": True, "stored": stored})


@app.get("/binaries/{path:path}")
def get_binary(path: str):
    if not settings.s3_bucket:
        raise HTTPException(status_code=500, detail="S3 bucket not configured (DM_S3_BUCKET).")

    key = f"{S3_BINARIES_PREFIX.rstrip('/')}/{path.lstrip('/')}"
    s3 = s3_client()

    if settings.binaries_mode == "presign":
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": key},
                ExpiresIn=settings.presign_ttl_seconds,
            )
            try:
                _log_device_connection(
                    action="BINARY_GET",
                    email="system@local",
                    client_uuid="00000000-0000-0000-0000-000000000000",
                    encryption_key_fingerprint="none",
                    source_ip=None,
                    user_agent=None,
                )
            except Exception:
                logger.exception("Failed to log binary call (presign)")
            return RedirectResponse(url=url, status_code=302)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Binary not found or cannot presign: {e!r}")

    if settings.binaries_mode == "proxy":
        try:
            obj = s3.get_object(Bucket=settings.s3_bucket, Key=key)
            body_stream = obj["Body"]
            content_type = obj.get("ContentType") or "application/octet-stream"

            def iterfile():
                for chunk in iter(lambda: body_stream.read(1024 * 1024), b""):
                    yield chunk

            try:
                _log_device_connection(
                    action="BINARY_GET",
                    email="system@local",
                    client_uuid="00000000-0000-0000-0000-000000000000",
                    encryption_key_fingerprint="none",
                    source_ip=None,
                    user_agent=None,
                )
            except Exception:
                logger.exception("Failed to log binary call (proxy)")
            return StreamingResponse(iterfile(), media_type=content_type)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Binary not found: {e!r}")

    raise HTTPException(status_code=500, detail="Invalid DM_BINARIES_MODE (must be presign or proxy).")

# ---- Local entrypoint (VS Code friendly)
# Allows debugging by running this file directly (e.g. VS Code: "Python: Current File").
# In production, prefer: uvicorn app.main:app --host 0.0.0.0 --port 3001

def _get_port() -> int:
    try:
        return int(os.getenv("DM_PORT", os.getenv("PORT", "8000")))
    except ValueError:
        return 8000


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_get_port(),
        reload=os.getenv("RELOAD", "false").lower() in ("1", "true", "yes"),
        log_level=os.getenv("LOG_LEVEL", "info"),
    )


@app.on_event("startup")
def _startup_db_init() -> None:
    if psycopg2 is None:
        logger.warning("psycopg2 not installed; skipping DB bootstrap/schema init")
        return
    base_url = _db_url()
    if not base_url:
        return
    try:
        admin_url = _admin_db_url(base_url)
        if not admin_url:
            raise RuntimeError("No admin database URL available")
        admin_url = _with_db(admin_url, "postgres")
        _wait_for_db(admin_url, timeout_seconds=30, interval_seconds=1.0)
        try:
            _ensure_dev_role(admin_url)
        except psycopg2.Error:
            logger.warning("Skipping dev role creation/alter (insufficient privilege)")
        _ensure_database_exists(admin_url, "bootstrap")
    except Exception:
        logger.exception("Failed to ensure database bootstrap exists")
    try:
        bootstrap_url = _db_url_bootstrap()
        if bootstrap_url and admin_url:
            admin_bootstrap_url = _with_db(admin_url, "bootstrap")
            _wait_for_db(admin_bootstrap_url, timeout_seconds=30, interval_seconds=1.0)
            _apply_schema(admin_bootstrap_url)
            _ensure_dev_privileges(admin_bootstrap_url)
    except Exception:
        logger.exception("Failed to apply DB schema")
