from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
import uuid
import base64
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib import request as urllib_request
from urllib import error as urllib_error

from fastapi import FastAPI, File, Request, Response, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

import httpx
import uvicorn
import boto3
from botocore.client import Config

try:
    import psycopg2  # type: ignore
    import psycopg2.pool  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    psycopg2 = None  # type: ignore

try:
    import jwt  # type: ignore
    from jwt import PyJWKClient  # type: ignore
    from jwt.exceptions import PyJWTError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    jwt = None  # type: ignore
    PyJWKClient = None  # type: ignore
    PyJWTError = Exception  # type: ignore

if os.getenv("RELOAD", "").lower() == "true" and "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"

from .settings import settings
from .s3 import s3_client
from .postgres_queue import PostgresQueue, QueueJob

app = FastAPI(title="Device Management API", version="0.1.0")
logger = logging.getLogger("device-management")

_RUNTIME_MODE = str(settings.runtime_mode or "api").strip().lower()

# ---- Admin UI router (Jinja2 + HTMX, no external JS build)
if _RUNTIME_MODE in ("admin", "all"):
    from fastapi.staticfiles import StaticFiles
    from .admin.router import router as admin_router
    app.include_router(admin_router, prefix="/admin")
    _admin_static = os.path.join(os.path.dirname(__file__), "admin", "static")
    if os.path.isdir(_admin_static):
        app.mount("/admin/static", StaticFiles(directory=_admin_static), name="admin-static")

# ---- Security headers middleware (admin UI + API)
# Paths whose mutation (POST/PUT/PATCH/DELETE) invalidates the config cache.
_CACHE_INVALIDATION_PATH_PREFIXES = (
    "/admin/catalog",       # plugin CRUD, versions, env overrides, access, keycloak
    "/admin/campaigns",     # campaign create / activate / pause / resume / abort
    "/admin/deploy",        # deploy create / pause / resume / abort / complete
    "/api/catalog",         # REST API plugin & config-template updates
    "/api/campaigns",       # REST API campaign lifecycle
    "/api/artifacts",       # artifact uploads
    "/api/keycloak",        # keycloak client changes
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    # Auto-invalidate config cache on successful mutations
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if response.status_code < 400 and any(path.startswith(p) for p in _CACHE_INVALIDATION_PATH_PREFIXES):
            _config_cache_clear()

    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path.startswith("/admin"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "font-src 'self' https://cdn.jsdelivr.net;"
        )
    return response

# ---- CORS
origins = [o.strip() for o in settings.allow_origins.split(",")] if settings.allow_origins else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

MAX_BODY_BYTES = settings.max_body_size_mb * 1024 * 1024
TELEMETRY_MAX_BODY_BYTES = settings.telemetry_max_body_size_mb * 1024 * 1024
S3_BINARIES_PREFIX = settings.s3_prefix_binaries
_telemetry_signing_warning_emitted = False
_queue_manager: PostgresQueue | None = None
_queue_lock = threading.Lock()
_embedded_worker_thread: threading.Thread | None = None
_embedded_worker_stop: threading.Event | None = None


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# Supports env-var placeholders in config templates.
# Preferred syntax: ${{VARNAME}}
# Backward-compatible syntax: ${VARNAME}
_TEMPLATE_VAR_RE = re.compile(r"\$\{\{([A-Z0-9_]+)\}\}|\$\{([A-Z0-9_]+)\}")


def _repo_root() -> str:
    # app/ is a package folder; repo root is one level above
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


SCHEMA_SQL_PATH = os.path.join(_repo_root(), "db", "schema.sql")


def _db_url() -> str | None:
    return os.getenv("DATABASE_URL") or settings.database_url or None


def _db_url_bootstrap() -> str | None:
    base = _db_url()
    if not base:
        return None
    return _with_db(base, "bootstrap")


# ---- Connection pool (P0 performance) ----
_pool: Any = None
_pool_lock = threading.Lock()
_POOL_MIN = 2
_POOL_MAX = 10


def _get_pool():
    """Return (or lazily create) a ThreadedConnectionPool for the bootstrap DB."""
    global _pool
    if _pool is not None:
        return _pool
    if psycopg2 is None:
        return None
    db_url = _db_url_bootstrap() or _db_url()
    if not db_url:
        return None
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, db_url)
        except Exception as exc:
            logger.warning("Connection pool creation failed: %s", exc)
            return None
    return _pool


class _PoolConn:
    """Context manager: borrows a connection from the pool, returns it on exit."""
    __slots__ = ("_conn", "_pool")

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()
        self._conn.autocommit = True

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        try:
            self._pool.putconn(self._conn)
        except Exception:
            pass
        self._conn = None


def _pooled_conn():
    """Return a _PoolConn context manager, or None if pool unavailable."""
    pool = _get_pool()
    if pool is None:
        return None
    return _PoolConn(pool)


# ---- Config response cache (P2 performance) ----
_CONFIG_CACHE: dict[str, tuple[float, dict]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE_TTL = 60.0  # seconds


def _config_cache_get(key: str) -> dict | None:
    """Return cached config response or None if expired/missing."""
    with _CONFIG_CACHE_LOCK:
        entry = _CONFIG_CACHE.get(key)
        if entry and entry[0] > time.time():
            return entry[1]
    return None


def _config_cache_set(key: str, value: dict) -> None:
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[key] = (time.time() + _CONFIG_CACHE_TTL, value)


def _pull_binary_from_admin(s3_path: str) -> bool:
    """Pull a binary from the admin pod's files API and cache it locally.

    Called on download-miss: the API pod doesn't have the file yet.
    """
    token = (settings.queue_admin_token or "").strip()
    admin_url = os.getenv("DM_ADMIN_INTERNAL_URL", "http://device-management-admin").rstrip("/")
    if not token:
        return False
    # Extract relative path from s3_path (strip any /data/content/binaries/ or /data/binaries/ prefix)
    rel = s3_path
    for prefix in ("/data/content/binaries/", "/data/binaries/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
            break
    else:
        rel = rel.lstrip("/")
    url = f"{admin_url}/admin/api/files/{rel}"
    try:
        logger.info("pull_binary_from_admin: fetching %s", url)
        resp = httpx.get(url, headers={"x-admin-token": token}, timeout=60, follow_redirects=True)
        logger.info("pull_binary_from_admin: got %d (%d bytes)", resp.status_code, len(resp.content))
        if resp.status_code == 200 and resp.content:
            os.makedirs(os.path.dirname(s3_path), exist_ok=True)
            with open(s3_path, "wb") as f:
                f.write(resp.content)
            logger.info("pull_binary_from_admin: cached %s (%d bytes)", rel, len(resp.content))
            return True
        logger.warning("pull_binary_from_admin: unexpected status %d for %s", resp.status_code, url)
    except Exception as exc:
        logger.warning("pull_binary_from_admin: %s failed: %s", url, exc)
    return False


def _config_cache_clear() -> None:
    """Flush the entire config cache (call after deploy)."""
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.clear()


def _queue_db_url() -> str | None:
    return _db_url_bootstrap() or _db_url()


def _get_queue_manager() -> PostgresQueue | None:
    global _queue_manager
    if not settings.queue_enabled:
        return None
    if psycopg2 is None:
        return None
    dsn = _queue_db_url()
    if not dsn:
        return None
    with _queue_lock:
        if _queue_manager is not None:
            return _queue_manager
        try:
            _queue_manager = PostgresQueue(
                dsn,
                lock_ttl_seconds=settings.queue_lock_ttl_seconds,
                default_max_attempts=settings.queue_default_max_attempts,
                retry_base_seconds=settings.queue_retry_base_seconds,
                retry_max_seconds=settings.queue_retry_max_seconds,
                retry_jitter_seconds=settings.queue_retry_jitter_seconds,
            )
        except Exception as exc:
            logger.warning("Queue manager unavailable: %s", exc)
            return None
        return _queue_manager


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
            # Migrations BEFORE schema.sql: add columns that CREATE TABLE IF
            # NOT EXISTS won't add to existing tables.  Must run first so that
            # indexes in schema.sql referencing these columns don't fail.
            cur.execute("""
                DO $$ BEGIN
                  -- campaigns: add environment, plugin_id, version_id if missing
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'campaigns') THEN
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'campaigns' AND column_name = 'environment'
                    ) THEN
                      ALTER TABLE campaigns ADD COLUMN environment VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'campaigns' AND column_name = 'plugin_id'
                    ) THEN
                      ALTER TABLE campaigns ADD COLUMN plugin_id INT REFERENCES plugins(id);
                    END IF;
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'campaigns' AND column_name = 'version_id'
                    ) THEN
                      ALTER TABLE campaigns ADD COLUMN version_id INT REFERENCES plugin_versions(id);
                    END IF;
                  END IF;
                  -- plugins: replace absolute UNIQUE(slug) with partial index
                  IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'plugins_slug_key' AND conrelid = 'plugins'::regclass
                  ) THEN
                    ALTER TABLE plugins DROP CONSTRAINT plugins_slug_key;
                  END IF;
                END $$;
            """)
            # Now apply the full schema (CREATE TABLE IF NOT EXISTS + indexes)
            cur.execute(sql)
            # Ensure the partial unique index exists (idempotent)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_plugins_slug_active
                ON plugins(slug) WHERE status <> 'removed'
            """)
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
    client_uuid = (
        request.headers.get("X-Client-UUID")
        or (body_obj or {}).get("client_uuid")
        or (body_obj or {}).get("plugin_uuid")
        or "00000000-0000-0000-0000-000000000000"
    )
    fingerprint = request.headers.get("X-Encryption-Key-Fingerprint") or (body_obj or {}).get("encryption_key_fingerprint") or "unknown"
    return email, client_uuid, fingerprint


def _validate_enroll_payload(body_obj: dict) -> list[str]:
    missing: list[str] = []
    for field in ("device_name", "plugin_uuid"):
        val = body_obj.get(field)
        if not isinstance(val, str) or not val.strip():
            missing.append(field)
    return missing


def _upsert_provisioning(*, email: str, client_uuid: str, device_name: str, encryption_key: str) -> None:
    if psycopg2 is None:
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
                INSERT INTO provisioning (email, device_name, client_uuid, status, encryption_key, comments)
                VALUES (%s, %s, %s, 'ENROLLED', %s, %s)
                ON CONFLICT (client_uuid) WHERE status IN ('PENDING', 'ENROLLED')
                DO UPDATE SET
                    email = EXCLUDED.email,
                    device_name = EXCLUDED.device_name,
                    status = 'ENROLLED',
                    encryption_key = EXCLUDED.encryption_key,
                    updated_at = now()
                """,
                (email, device_name, client_uuid, encryption_key, "enroll"),
            )
    finally:
        conn.close()


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


# Legacy fallback — only used when DB is unreachable
_DEVICE_TYPE_FALLBACK = {"matisse", "libreoffice", "mirai-libreoffice"}


def _resolve_device(device: str, cur) -> tuple[str | None, str | None, int | None, str]:
    """Resolve slug/alias → (device_name, device_type, plugin_id, resolved_via).

    resolved_via: 'slug' | 'alias' | 'fallback' | 'unknown'
    """
    # 1. Slug exact (= device_name)
    try:
        cur.execute(
            "SELECT slug, device_type, id FROM plugins WHERE slug = %s AND status = 'active'",
            (device,),
        )
        row = cur.fetchone()
        if row:
            return row[0], row[1], row[2], "slug"
    except Exception:
        pass

    # 2. Alias
    try:
        cur.execute("""
            SELECT p.slug, p.device_type, p.id
            FROM plugin_aliases a JOIN plugins p ON p.id = a.plugin_id
            WHERE a.alias = %s AND p.status = 'active'
        """, (device,))
        row = cur.fetchone()
        if row:
            return row[0], row[1], row[2], "alias"
    except Exception:
        pass

    # 3. Fallback legacy device_type (no catalog entry)
    if device in _DEVICE_TYPE_FALLBACK:
        return device, device, None, "fallback"

    return None, None, None, "unknown"


def _log_alias_access(cur, *, alias: str, slug: str, plugin_id: int,
                      client_uuid: str = "", source_ip: str | None = None):
    """Log alias-based config access for migration tracking."""
    try:
        cur.execute("""
            INSERT INTO alias_access_log (alias, slug, plugin_id, client_uuid, source_ip)
            VALUES (%s, %s, %s, %s, %s::inet)
        """, (alias, slug, plugin_id, client_uuid or None, source_ip))
    except Exception:
        pass  # best-effort logging


def _apply_catalog_overrides(cfg: dict, *, plugin_id: int, profile: str, cur) -> dict:
    """Apply plugin-specific env overrides + Keycloak client from catalog."""
    config_obj = cfg.get("config")
    if not isinstance(config_obj, dict):
        return cfg

    # Env overrides
    try:
        cur.execute("""
            SELECT key, value FROM plugin_env_overrides
            WHERE plugin_id = %s AND environment = %s
        """, (plugin_id, profile))
        for key, value in cur.fetchall():
            config_obj[key] = value
    except Exception:
        pass

    # Keycloak client
    try:
        cur.execute("""
            SELECT kc.client_id, kc.realm
            FROM plugin_keycloak_clients pkc
            JOIN keycloak_clients kc ON kc.id = pkc.keycloak_client_id
            WHERE pkc.plugin_id = %s AND pkc.environment = %s LIMIT 1
        """, (plugin_id, profile))
        kc = cur.fetchone()
        if kc:
            config_obj["keycloakClientId"] = kc[0]
            config_obj["keycloakRealm"] = kc[1]
    except Exception:
        pass

    return cfg


def _check_plugin_access(plugin_row: dict | None, request: Request, cur) -> bool:
    """Check if the caller has access to a restricted plugin. Returns True if access OK."""
    if not plugin_row:
        return True
    access_mode = plugin_row.get("access_mode", "open")
    if access_mode == "open":
        return True

    if access_mode == "keycloak_group":
        required = plugin_row.get("required_group", "")
        if not required:
            return True
        # Try to extract groups from Bearer token (unverified, best-effort)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                token_str = auth[7:]
                payload_b64 = token_str.split(".")[1] + "=="
                claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                user_groups = claims.get("groups", [])
                return required in user_groups
            except Exception:
                pass
        return False

    if access_mode == "waitlist":
        email = request.headers.get("X-User-Email", "").strip()
        if email:
            try:
                cur.execute("""
                    SELECT 1 FROM plugin_waitlist
                    WHERE plugin_id = %s AND email = %s AND status = 'approved'
                """, (plugin_row["id"], email))
                return cur.fetchone() is not None
            except Exception:
                pass
        return False

    return True


def _build_config_from_template(template: dict, profile: str) -> dict:
    """Merge default + profile section from a dm-config.json template."""
    default = dict(template.get("default", {}))
    env_section = template.get(profile, {})
    if isinstance(env_section, dict):
        merged = {**default, **env_section}
    else:
        merged = default
    # Remove inline documentation fields
    merged.pop("_description", None)
    return {"configVersion": template.get("configVersion", 1), "config": merged}


def _load_config_template(profile: str, device: str | None = None,
                          device_name: str | None = None,
                          cur=None) -> dict:
    """Load a config template — DB first, then filesystem fallback.

    Resolution order:
    1. DB: plugins.config_template (dm-config.json format, merged default+profile)
    2. File: config/<device_name>/config.<profile>.json (legacy)
    3. File: config/<device>/config.<profile>.json
    4. File: config/config.<profile>.json
    """
    # 1. Try DB (dm-config.json stored in plugins.config_template)
    if cur:
        try:
            slug = device_name or device
            if slug:
                cur.execute(
                    "SELECT config_template FROM plugins WHERE (slug = %s OR device_type = %s) AND status <> 'removed' LIMIT 1",
                    (slug, device or slug),
                )
                row = cur.fetchone()
                if row and row[0]:
                    template = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    result = _build_config_from_template(template, profile)
                    logger.info("Config loaded from DB (plugins.config_template) for %s profile=%s", slug, profile)
                    return result
        except Exception as e:
            logger.warning("DB config_template load failed (non-fatal): %s", e)

    # 2. Filesystem fallback (legacy)
    bases: list[str] = []
    if settings.config_dir:
        bases.append(settings.config_dir)
    bases.append(os.path.join(_repo_root(), "config"))

    for base in bases:
        candidates = []
        if device_name and device_name != device:
            candidates.extend([
                os.path.join(base, device_name, f"config.{profile}.json"),
                os.path.join(base, device_name, "config.json"),
            ])
        if device:
            candidates.extend([
                os.path.join(base, device, f"config.{profile}.json"),
                os.path.join(base, device, "config.json"),
            ])
        candidates.extend([
            os.path.join(base, f"config.{profile}.json"),
            os.path.join(base, "config.json"),
        ])
        for p in candidates:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)

    # No DB template and no filesystem fallback — return a minimal empty config
    # This happens when no device is specified or the plugin has no config_template yet
    logger.warning("No config template found for device=%s device_name=%s profile=%s — returning minimal config",
                   device, device_name, profile)
    return {"configVersion": 1, "config": {}}


def _safe_path_join(base_dir: str, relative_path: str) -> str:
    base_abs = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base_abs, relative_path.lstrip("/")))
    if candidate == base_abs or candidate.startswith(base_abs + os.sep):
        return candidate
    raise HTTPException(status_code=400, detail="Invalid path")


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


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


def _resolve_public_telemetry_endpoint() -> str:
    endpoint = (settings.telemetry_public_endpoint or "").strip() or "/telemetry/v1/traces"
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    public_base = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if public_base:
        parsed = urlparse(public_base)
        if parsed.scheme and parsed.netloc:
            # Telemetry uses its own Bearer token auth — no need for the relay proxy.
            return f"{parsed.scheme}://{parsed.netloc}{endpoint}"
    return endpoint


def _mint_telemetry_token(*, device: str | None, profile: str) -> tuple[str, int | None]:
    secret = (settings.telemetry_token_signing_key or "").strip()
    if not secret:
        return "", None

    now = int(time.time())
    ttl = max(30, int(settings.telemetry_token_ttl_seconds))
    payload = {
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl,
        "profile": profile,
        "device": device or "unknown",
    }
    payload_raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(payload_raw)
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    token = f"{payload_b64}.{_b64url_encode(sig)}"
    return token, int(payload["exp"])


def _verify_telemetry_token(token: str) -> dict:
    secret = (settings.telemetry_token_signing_key or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Telemetry token verification key is not configured.")

    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed telemetry token.")

    expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    try:
        provided_sig = _b64url_decode(sig_b64)
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed telemetry token signature.")
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(status_code=401, detail="Invalid telemetry token signature.")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed telemetry token payload.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Malformed telemetry token payload.")

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp <= now:
        raise HTTPException(status_code=401, detail="Telemetry token expired.")
    return payload


def _extract_bearer_token(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth[7:].strip()


_extract_access_token_from_request = _extract_bearer_token


def _queue_admin_guard(request: Request) -> None:
    expected = str(settings.queue_admin_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Queue admin token is not configured.")
    provided = str(request.headers.get("x-queue-admin-token") or "").strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Missing or invalid queue admin token.")


def _queue_worker_id(role: str) -> str:
    host = socket.gethostname() or "unknown-host"
    return f"{role}:{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


_SECRET_CONFIG_KEYS = {
    "llm_api_tokens",
    "tokenOWUI",
    "telemetryKey",
    "keycloak_client_secret",
    "keycloakClientSecret",
}

_RELAY_MEMORY_STORE: dict[str, dict] = {}
_AUTH_JWKS_CLIENT_CACHE: dict[str, tuple[float, Any]] = {}
_AUTH_JWKS_URI_CACHE: dict[str, tuple[float, str]] = {}
_AUTH_CACHE_LOCK = threading.Lock()

# ---- Keycloak group membership cache (for cohort resolution)
# Structure: {group_name: (expiry_timestamp, set_of_emails)}
_KC_GROUP_CACHE: dict[str, tuple[float, set]] = {}
_KC_GROUP_CACHE_TTL = 300.0  # 5 minutes
_KC_GROUP_CACHE_LOCK = threading.Lock()


# ---- Enriched config helpers

def _parse_version_tuple(v: str) -> tuple:
    """Parse a version string into a tuple of ints for comparison.

    Supports any number of segments (semver 3, or extended 4-5 segments).
    """
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def _infer_platform_variant(platform_type: str, platform_version: str, manifest_version: int | None) -> str | None:
    """Infer a platform variant string from platform type/version/manifest."""
    if platform_type == "thunderbird":
        v = _parse_version_tuple(platform_version)
        if v < (78, 0, 0):
            return "tb60"
        if v < (128, 0, 0):
            return "tb78"
        return "tb128"
    if platform_type in ("chrome", "edge"):
        return f"mv{manifest_version}" if manifest_version else "mv3"
    return None


def _resolve_device_cohorts(cur, *, email: str, client_uuid: str) -> list[int]:
    """Return a list of cohort IDs the device belongs to.

    Gracefully returns [] if the migration tables don't exist yet.
    Uses a single JOIN to resolve manual cohorts instead of N+1 queries.
    """
    try:
        cur.execute("SELECT id, type, config FROM cohorts")
        cohorts = cur.fetchall()
    except Exception:
        return []

    if not cohorts:
        return []

    # Batch-resolve manual cohorts in a single query (fixes N+1)
    manual_ids = [row[0] for row in cohorts if row[1] == "manual"]
    manual_matched: set[int] = set()
    if manual_ids:
        try:
            placeholders = ",".join(["%s"] * len(manual_ids))
            cur.execute(
                f"""
                SELECT DISTINCT cohort_id FROM cohort_members
                WHERE cohort_id IN ({placeholders})
                  AND (
                    (identifier_type = 'email' AND identifier_value = %s)
                    OR (identifier_type = 'client_uuid' AND identifier_value = %s)
                  )
                """,
                [*manual_ids, email, client_uuid],
            )
            manual_matched = {row[0] for row in cur.fetchall()}
        except Exception:
            pass

    matched: list[int] = []
    for row in cohorts:
        cohort_id, ctype, cconfig = row[0], row[1], row[2] or {}
        if isinstance(cconfig, str):
            try:
                cconfig = json.loads(cconfig)
            except Exception:
                cconfig = {}

        if ctype == "manual":
            if cohort_id in manual_matched:
                matched.append(cohort_id)

        elif ctype == "percentage":
            pct = int(cconfig.get("percentage", 0))
            if pct > 0 and client_uuid:
                digest = hashlib.sha256(client_uuid.encode()).hexdigest()
                if int(digest, 16) % 100 < pct:
                    matched.append(cohort_id)

        elif ctype == "email_pattern":
            pattern = str(cconfig.get("pattern", ""))
            if pattern:
                try:
                    if re.match(pattern, email or "", re.IGNORECASE):
                        matched.append(cohort_id)
                except Exception:
                    pass

        elif ctype == "keycloak_group":
            group_name = str(cconfig.get("group_name", ""))
            if group_name and email:
                now = time.time()
                group_emails: set | None = None
                with _KC_GROUP_CACHE_LOCK:
                    cached = _KC_GROUP_CACHE.get(group_name)
                    if cached and cached[0] > now:
                        group_emails = cached[1]
                if group_emails is None:
                    # No live fetch in this implementation — cache miss yields empty set
                    group_emails = set()
                    with _KC_GROUP_CACHE_LOCK:
                        _KC_GROUP_CACHE[group_name] = (now + _KC_GROUP_CACHE_TTL, group_emails)
                if email.lower() in {e.lower() for e in group_emails}:
                    matched.append(cohort_id)

    return matched


def _resolve_feature_flags(cur, *, device_cohort_ids: list[int], plugin_version: str) -> dict:
    """Compute the effective feature flags dict for the device."""
    try:
        cur.execute("SELECT name, default_value FROM feature_flags")
        flags: dict[str, bool] = {row[0]: bool(row[1]) for row in cur.fetchall()}
    except Exception:
        return {}

    if not flags:
        return {}

    if device_cohort_ids:
        try:
            placeholders = ",".join(["%s"] * len(device_cohort_ids))
            cur.execute(
                f"""
                SELECT ff.name, ffo.value, ffo.min_plugin_version
                FROM feature_flag_overrides ffo
                JOIN feature_flags ff ON ff.id = ffo.feature_id
                WHERE ffo.cohort_id IN ({placeholders})
                """,
                device_cohort_ids,
            )
            overrides = cur.fetchall()
        except Exception:
            overrides = []

        for row in overrides:
            flag_name, override_val, min_pv = row[0], bool(row[1]), row[2]
            if min_pv and plugin_version:
                if _parse_version_tuple(plugin_version) < _parse_version_tuple(min_pv):
                    continue  # plugin too old, skip this override
            # false wins: once false, stays false
            if flag_name in flags:
                flags[flag_name] = flags[flag_name] and override_val

    return flags


def _resolve_active_campaign(cur, *, device_cohort_ids: list[int], device_type: str, platform_version: str) -> dict | None:
    """Find the best active plugin_update campaign for the device, or None."""
    try:
        cohort_filter = list(device_cohort_ids) if device_cohort_ids else []
        cur.execute(
            """
            SELECT
              c.id, c.urgency, c.deadline_at, c.target_cohort_id,
              a.version  AS artifact_version,
              a.s3_path  AS artifact_s3_path,
              a.checksum AS artifact_checksum,
              a.changelog_url,
              a.min_host_version,
              a.max_host_version,
              ra.s3_path  AS rollback_s3_path,
              ra.version  AS rollback_version,
              ra.checksum AS rollback_checksum,
              c.rollout_config,
              c.created_at AS campaign_created_at
            FROM campaigns c
            LEFT JOIN artifacts a  ON a.id  = c.artifact_id
            LEFT JOIN artifacts ra ON ra.id = c.rollback_artifact_id
            WHERE c.status = 'active'
              AND c.type   = 'plugin_update'
              AND (c.target_cohort_id IS NULL OR c.target_cohort_id = ANY(%s))
            ORDER BY c.created_at DESC
            LIMIT 1
            """,
            (cohort_filter if cohort_filter else [None],),
        )
        row = cur.fetchone()
    except Exception:
        return None

    if not row:
        return None

    (
        camp_id, urgency, deadline_at, target_cohort_id,
        artifact_version, artifact_s3_path, artifact_checksum, changelog_url,
        min_host_version, max_host_version,
        rollback_s3_path, rollback_version, rollback_checksum,
        rollout_config, campaign_created_at,
    ) = row

    # Filter by host (platform) version compatibility
    if platform_version and min_host_version:
        if _parse_version_tuple(platform_version) < _parse_version_tuple(min_host_version):
            return None
    if platform_version and max_host_version:
        if _parse_version_tuple(platform_version) >= _parse_version_tuple(max_host_version):
            return None

    deadline_iso: str | None = None
    if deadline_at is not None:
        try:
            if hasattr(deadline_at, "isoformat"):
                deadline_iso = deadline_at.isoformat()
            else:
                deadline_iso = str(deadline_at)
        except Exception:
            deadline_iso = None

    return {
        "campaign_id": camp_id,
        "urgency": str(urgency or "normal"),
        "deadline_iso": deadline_iso,
        "artifact_version": str(artifact_version or ""),
        "artifact_s3_path": str(artifact_s3_path or ""),
        "artifact_checksum": str(artifact_checksum or ""),
        "changelog_url": changelog_url,
        "rollback_s3_path": rollback_s3_path,
        "rollback_version": str(rollback_version or ""),
        "rollback_checksum": str(rollback_checksum or ""),
        "rollout_config": rollout_config if isinstance(rollout_config, dict) else None,
        "campaign_created_at": campaign_created_at,
    }


def _get_current_rollout_percent(campaign: dict, stages: list) -> int:
    """Return the active rollout percent based on elapsed time since campaign start."""
    import datetime
    start = campaign.get("campaign_created_at")
    if start is None:
        return 100
    if hasattr(start, "timestamp"):
        start_ts = start.timestamp()
    else:
        try:
            start_ts = datetime.datetime.fromisoformat(str(start)).timestamp()
        except Exception:
            return 100
    elapsed_hours = (time.time() - start_ts) / 3600
    cumulative_hours = 0
    for stage in stages:
        cumulative_hours += stage.get("duration_hours", 0)
        if elapsed_hours < cumulative_hours or stage.get("percent", 100) == 100:
            return stage.get("percent", 100)
    return 100


def _build_update_directive(
    *,
    plugin_version: str,
    campaign: dict | None,
    client_uuid: str = "",
    device_name: str = "",
) -> dict | None:
    """Build the update directive dict, or return None if no action needed."""
    if not plugin_version or plugin_version in ("unknown", "0", "") or not campaign:
        return None

    artifact_version = campaign["artifact_version"]
    if not artifact_version:
        return None

    if plugin_version == artifact_version:
        return None

    pv = _parse_version_tuple(plugin_version)
    av = _parse_version_tuple(artifact_version)

    # Check rollout percentage gating
    rollout_config = campaign.get("rollout_config")
    if rollout_config and isinstance(rollout_config, dict) and client_uuid:
        stages = rollout_config.get("stages", [])
        if stages:
            current_percent = _get_current_rollout_percent(campaign, stages)
            if current_percent < 100:
                device_hash = int(hashlib.md5(client_uuid.encode()).hexdigest()[:8], 16) % 100
                if device_hash >= current_percent:
                    return None  # Not yet eligible for this rollout stage

    # Prefer catalog download URL (human-friendly, handles redirects)
    # Fallback to raw binary path if device_name is unknown
    def _artifact_url(s3_path):
        if device_name:
            return f"/catalog/{device_name}/download"
        return "/binaries/" + str(s3_path or "").lstrip("/")

    if pv < av:
        return {
            "action": "update",
            "current_version": plugin_version,
            "target_version": artifact_version,
            "artifact_url": _artifact_url(campaign["artifact_s3_path"]),
            "checksum": campaign["artifact_checksum"],
            "urgency": campaign["urgency"],
            "changelog_url": campaign["changelog_url"],
            "deadline_at": campaign["deadline_iso"],
            "campaign_id": campaign["campaign_id"],
        }

    # plugin_version > artifact_version → possible rollback
    if campaign["rollback_s3_path"] and campaign["rollback_version"]:
        return {
            "action": "rollback",
            "current_version": plugin_version,
            "target_version": campaign["rollback_version"],
            "artifact_url": _artifact_url(campaign["rollback_s3_path"]),
            "checksum": campaign["rollback_checksum"],
            "urgency": campaign["urgency"],
            "changelog_url": campaign["changelog_url"],
            "deadline_at": campaign["deadline_iso"],
            "campaign_id": campaign["campaign_id"],
        }

    return None


def _upsert_campaign_device_status(cur, *, campaign_id: int, client_uuid: str, email: str, version_before: str) -> None:
    """UPSERT the campaign_device_status row (fire-and-forget, errors swallowed by caller)."""
    cur.execute(
        """
        INSERT INTO campaign_device_status
          (campaign_id, client_uuid, email, status, version_before, last_contact_at)
        VALUES (%s, %s, %s, 'notified', %s, NOW())
        ON CONFLICT (campaign_id, client_uuid) DO UPDATE
          SET status = 'notified',
              version_before = EXCLUDED.version_before,
              last_contact_at = NOW()
        """,
        (campaign_id, client_uuid, email or None, version_before or None),
    )


def _normalize_client_uuid(raw_value: str | None) -> str:
    raw = str(raw_value or "").strip()
    if not raw:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(raw))
    except Exception:
        # Keep deterministic fallback for non-UUID plugin ids.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _parse_unverified_jwt_payload(token: str) -> dict:
    if not token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_raw = _b64url_decode(parts[1]).decode("utf-8")
        data = json.loads(payload_raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalized_url(url: str | None) -> str:
    return str(url or "").strip().rstrip("/")


def _resolve_auth_issuer_url() -> str:
    # settings.auth_issuer_url already resolves AUTH_ISSUER_URL → KEYCLOAK_ISSUER_URL via _env_default.
    return _normalized_url(settings.auth_issuer_url)


def _resolve_auth_audience() -> str:
    # settings.auth_audience already resolves AUTH_AUDIENCE → KEYCLOAK_CLIENT_ID via _env_default.
    return settings.auth_audience.strip()


def _resolve_allowed_auth_algorithms() -> list[str]:
    raw = settings.auth_allowed_algorithms_csv.strip()
    values = [a.strip() for a in raw.split(",") if a.strip()]
    return values or ["RS256"]


def _resolve_jwks_uri(issuer: str) -> str:
    explicit_jwks_url = str(settings.auth_jwks_url or "").strip()
    if explicit_jwks_url:
        return explicit_jwks_url

    now = time.time()
    ttl = max(60, int(settings.auth_jwks_cache_ttl_seconds or 0))
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_JWKS_URI_CACHE.get(issuer)
        if cached and cached[0] > now:
            return cached[1]

    # Try OIDC discovery first.
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    jwks_uri = ""
    try:
        req = urllib_request.Request(
            discovery_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib_request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, dict):
                jwks_uri = str(payload.get("jwks_uri") or "").strip()
    except Exception:
        jwks_uri = ""

    # Keycloak fallback when discovery is unavailable.
    if not jwks_uri:
        jwks_uri = f"{issuer}/protocol/openid-connect/certs"

    with _AUTH_CACHE_LOCK:
        _AUTH_JWKS_URI_CACHE[issuer] = (now + ttl, jwks_uri)
    return jwks_uri


def _get_jwks_client(issuer: str):
    if PyJWKClient is None:
        raise HTTPException(status_code=503, detail="JWT verification backend is unavailable.")

    now = time.time()
    ttl = max(60, int(settings.auth_jwks_cache_ttl_seconds or 0))
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_JWKS_CLIENT_CACHE.get(issuer)
        if cached and cached[0] > now:
            return cached[1]

    jwks_uri = _resolve_jwks_uri(issuer)
    client = PyJWKClient(jwks_uri, cache_keys=True)
    with _AUTH_CACHE_LOCK:
        _AUTH_JWKS_CLIENT_CACHE[issuer] = (now + ttl, client)
    return client


def _verify_access_token(token: str) -> dict:
    if not token:
        return {}

    if not settings.auth_verify_access_token:
        payload = _parse_unverified_jwt_payload(token)
        return payload if isinstance(payload, dict) else {}

    if jwt is None:
        raise HTTPException(status_code=503, detail="JWT verification backend is unavailable.")

    issuer = _resolve_auth_issuer_url()
    if not issuer:
        raise HTTPException(status_code=503, detail="Auth issuer URL is not configured.")
    audience = _resolve_auth_audience()
    algorithms = _resolve_allowed_auth_algorithms()

    try:
        jwks_client = _get_jwks_client(issuer)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=algorithms,
            audience=audience if audience else None,
            options={
                "verify_aud": bool(audience),
                "verify_exp": True,
                "verify_iat": False,
                "verify_nbf": True,
                "verify_iss": False,
            },
            leeway=max(0, int(settings.auth_leeway_seconds)),
        )
    except PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid PKCE access token.")
    except Exception as exc:
        logger.warning("JWT verification failed with backend error: %s: %s", exc.__class__.__name__, exc)
        raise HTTPException(status_code=503, detail="Access token verification service unavailable.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid PKCE access token.")
    token_issuer = _normalized_url(payload.get("iss"))
    if not token_issuer or token_issuer != issuer:
        raise HTTPException(status_code=401, detail="Invalid PKCE access token issuer.")
    return payload


def _email_from_access_token(token: str) -> str:
    payload = _verify_access_token(token)
    email = payload.get("email") or payload.get("preferred_username") or payload.get("sub")
    if isinstance(email, str):
        return email.strip()
    return ""


def _relay_allowed_targets() -> list[str]:
    raw = str(settings.relay_allowed_targets_csv or "").strip()
    targets = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not targets:
        targets = ["keycloak", "config", "telemetry"]
    if "config" not in targets:
        targets.append("config")
    if "telemetry" not in targets:
        targets.append("telemetry")
    return sorted(set(targets))


def _hash_relay_secret(relay_client_id: str, relay_key: str) -> str:
    pepper = str(settings.relay_secret_pepper or "")
    base = f"{relay_client_id}:{relay_key}:{pepper}".encode("utf-8")
    return hashlib.sha256(base).hexdigest()


def _mint_or_rotate_relay_credentials(*, client_uuid: str, email: str) -> dict:
    relay_client_id = f"rc_{uuid.uuid4().hex[:24]}"
    relay_client_key = _b64url_encode(os.urandom(32))
    relay_key_hash = _hash_relay_secret(relay_client_id, relay_client_key)
    allowed_targets = _relay_allowed_targets()
    ttl_seconds = max(60, int(settings.relay_key_ttl_seconds or 0))
    expires_at = int(time.time()) + ttl_seconds

    db_url = _db_url_bootstrap()
    if psycopg2 is not None and db_url:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE relay_clients
                    SET revoked_at = now(), comments = 'rotated'
                    WHERE client_uuid = %s AND revoked_at IS NULL
                    """,
                    (client_uuid,),
                )
                cur.execute(
                    """
                    INSERT INTO relay_clients (
                        client_uuid, email, relay_client_id, relay_key_hash,
                        allowed_targets, expires_at, comments
                    ) VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s)
                    """,
                    (
                        client_uuid,
                        email,
                        relay_client_id,
                        relay_key_hash,
                        allowed_targets,
                        expires_at,
                        "enroll",
                    ),
                )
        finally:
            conn.close()
    else:
        _RELAY_MEMORY_STORE[relay_client_id] = {
            "client_uuid": client_uuid,
            "email": email,
            "relay_key_hash": relay_key_hash,
            "allowed_targets": allowed_targets,
            "expires_at": expires_at,
            "revoked": False,
        }

    return {
        "client_id": relay_client_id,
        "client_key": relay_client_key,
        "allowed_targets": allowed_targets,
        "expires_at": expires_at,
        "ttl_seconds": ttl_seconds,
    }


def _verify_relay_credentials(relay_client_id: str, relay_key: str, target: str | None = None) -> tuple[bool, dict | str]:
    relay_client_id = str(relay_client_id or "").strip()
    relay_key = str(relay_key or "").strip()
    target_norm = str(target or "").strip().lower()
    if not relay_client_id or not relay_key:
        return False, "missing relay headers"

    now = int(time.time())
    row: dict | None = None

    db_url = _db_url_bootstrap()
    if psycopg2 is not None and db_url:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT client_uuid::text, email::text, relay_key_hash, allowed_targets,
                           EXTRACT(EPOCH FROM expires_at)::bigint, revoked_at
                    FROM relay_clients
                    WHERE relay_client_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (relay_client_id,),
                )
                item = cur.fetchone()
            if item:
                row = {
                    "client_uuid": item[0],
                    "email": item[1],
                    "relay_key_hash": item[2],
                    "allowed_targets": list(item[3] or []),
                    "expires_at": int(item[4] or 0),
                    "revoked": item[5] is not None,
                }
        finally:
            conn.close()
    else:
        row = _RELAY_MEMORY_STORE.get(relay_client_id)

    if not row:
        return False, "unknown relay client"
    if bool(row.get("revoked")):
        return False, "relay key revoked"

    expected_hash = str(row.get("relay_key_hash") or "")
    provided_hash = _hash_relay_secret(relay_client_id, relay_key)
    if not expected_hash or not hmac.compare_digest(expected_hash, provided_hash):
        return False, "invalid relay key"

    expires_at = int(row.get("expires_at") or 0)
    if expires_at and expires_at <= now:
        return False, "relay key expired"

    allowed_targets = [str(t).strip().lower() for t in (row.get("allowed_targets") or []) if str(t).strip()]
    effective_targets = sorted(set(allowed_targets))
    # Backward compatibility: existing credentials with 'config' are also allowed for telemetry relay.
    if "config" in effective_targets and "telemetry" not in effective_targets:
        effective_targets.append("telemetry")
        effective_targets.sort()

    if target_norm and effective_targets and target_norm not in effective_targets:
        return False, f"target '{target_norm}' not allowed"

    return True, {
        "client_uuid": row.get("client_uuid", ""),
        "email": row.get("email", ""),
        "allowed_targets": effective_targets,
        "expires_at": expires_at,
    }


def _relay_auth_from_request(
    request: Request,
    *,
    target: str | None = None,
    require_proxy_token: bool = False,
) -> tuple[bool, dict | str]:
    if not settings.relay_enabled:
        return False, "relay auth disabled"

    if require_proxy_token:
        expected = str(settings.relay_proxy_shared_token or "").strip()
        provided = str(request.headers.get("x-relay-proxy-token") or "").strip()
        if expected and not hmac.compare_digest(expected, provided):
            return False, "invalid relay proxy token"

    relay_client_id = request.headers.get("x-relay-client") or request.headers.get("x-client-id") or ""
    relay_key = request.headers.get("x-relay-key") or request.headers.get("x-client-key") or ""
    return _verify_relay_credentials(relay_client_id, relay_key, target=target)


_AUTH_REQUIRED_MSG = "Authentification requise. Effectuez un enrollment (POST /enroll) pour obtenir vos credentials relay."


def _scrub_secret_values(cfg: dict) -> dict:
    cfg_obj = dict(cfg)
    config_obj = cfg_obj.get("config")
    if not isinstance(config_obj, dict):
        return cfg_obj

    has_secrets = False
    for secret_key in _SECRET_CONFIG_KEYS:
        if secret_key in config_obj and config_obj[secret_key]:
            has_secrets = True
            config_obj[secret_key] = ""

    # legacy aliases
    if "authHeaderKey" in config_obj:
        config_obj["authHeaderKey"] = ""

    if has_secrets:
        config_obj["_auth_notice"] = _AUTH_REQUIRED_MSG

    cfg_obj["config"] = config_obj
    return cfg_obj

def _apply_overrides(cfg: dict, *, profile: str, device: str | None = None) -> dict:
    """Apply targeted overrides from env."""
    global _telemetry_signing_warning_emitted

    config_obj = cfg.get("config")
    if not isinstance(config_obj, dict):
        return cfg

    config_obj["telemetryEnabled"] = bool(settings.telemetry_enabled)
    config_obj["telemetryEndpoint"] = _resolve_public_telemetry_endpoint()
    config_obj["telemetryAuthorizationType"] = settings.telemetry_authorization_type

    public_base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if public_base:
        config_obj["relayAssistantBaseUrl"] = f"{public_base}/relay-assistant"

        # Keep Keycloak values from config by default.
        # Force relay endpoints only when explicitly enabled via DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS.
        if settings.relay_force_keycloak_endpoints:
            relay_keycloak_base = f"{public_base}/relay-assistant/keycloak"
            config_obj["keycloakIssuerUrl"] = relay_keycloak_base
            config_obj["keycloakAuthorizationEndpoint"] = f"{relay_keycloak_base}/protocol/openid-connect/auth"
            config_obj["keycloakTokenEndpoint"] = f"{relay_keycloak_base}/protocol/openid-connect/token"
            config_obj["keycloakUserinfoEndpoint"] = f"{relay_keycloak_base}/protocol/openid-connect/userinfo"

    token = ""
    expires_at: int | None = None
    if settings.telemetry_enabled and settings.telemetry_authorization_type.lower() == "bearer":
        token, expires_at = _mint_telemetry_token(device=device, profile=profile)
        if settings.telemetry_require_token and not token and not _telemetry_signing_warning_emitted:
            logger.warning(
                "Telemetry token rotation requested but DM_TELEMETRY_TOKEN_SIGNING_KEY is empty."
            )
            _telemetry_signing_warning_emitted = True

    config_obj["telemetryKey"] = token
    if expires_at is not None:
        config_obj["telemetryKeyExpiresAt"] = expires_at
        config_obj["telemetryKeyTtlSeconds"] = int(settings.telemetry_token_ttl_seconds)
    return cfg


def _forward_telemetry_to_upstream(body: bytes, *, content_type: str, user_agent: str | None) -> Response:
    endpoint = (settings.telemetry_upstream_endpoint or "").strip()
    if not endpoint:
        raise HTTPException(status_code=503, detail="Telemetry upstream endpoint is not configured.")

    headers: dict[str, str] = {"Content-Type": content_type or "application/json"}
    if user_agent:
        headers["User-Agent"] = user_agent

    upstream_auth_type = (settings.telemetry_upstream_auth_type or "").strip()
    upstream_key = (settings.telemetry_upstream_key or "").strip()
    if upstream_auth_type and upstream_key:
        headers["Authorization"] = f"{upstream_auth_type} {upstream_key}".strip()

    req = urllib_request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            response_ct = resp.headers.get("Content-Type", "application/json")
            return Response(content=payload, status_code=resp.status, headers={"Content-Type": response_ct})
    except urllib_error.HTTPError as e:
        payload = e.read()
        response_ct = e.headers.get("Content-Type", "application/json")
        return Response(content=payload, status_code=e.code, headers={"Content-Type": response_ct})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telemetry upstream unreachable: {e!r}")


def _enqueue_telemetry_payload(
    *,
    body: bytes,
    content_type: str,
    user_agent: str | None,
    client_uuid: str,
    dedupe_key: str | None = None,
) -> tuple[bool, str | None]:
    queue = _get_queue_manager()
    if not queue:
        return False, None
    payload = {
        "body_b64": _b64url_encode(body),
        "content_type": content_type or "application/json",
        "user_agent": user_agent or "",
        "client_uuid": client_uuid,
        "received_at": int(time.time()),
    }
    job_id, _status = queue.enqueue(
        topic="telemetry.forward",
        payload=payload,
        dedupe_key=dedupe_key,
    )
    return True, job_id


def _persist_enroll_side_effects(
    *,
    body: bytes,
    email: str,
    client_uuid: str,
    fingerprint: str,
    device_name: str,
    source_ip: str | None,
    user_agent: str | None,
) -> dict[str, str | bool]:
    epoch_ms = int(time.time() * 1000)
    rid = uuid.uuid4().hex
    fname = f"{epoch_ms}-{rid}.json"
    stored: dict[str, str | bool] = {}

    if settings.store_enroll_locally:
        _ensure_dir(settings.enroll_dir)
        path = os.path.join(settings.enroll_dir, fname)
        with open(path, "wb") as f:
            f.write(body)
        stored["local"] = path

    if settings.store_enroll_s3:
        if not settings.s3_bucket:
            raise RuntimeError("S3 bucket not configured (DM_S3_BUCKET).")
        key = f"{settings.s3_prefix_enroll.rstrip('/')}/{fname}"
        s3 = s3_client()
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        stored["s3"] = f"s3://{settings.s3_bucket}/{key}"

    encryption_key = fingerprint if fingerprint and fingerprint != "unknown" else "unknown"
    try:
        _upsert_provisioning(
            email=email,
            client_uuid=client_uuid,
            device_name=device_name,
            encryption_key=encryption_key,
        )
    except Exception:
        logger.exception("Failed to upsert provisioning")

    try:
        _log_device_connection(
            action="ENROLL",
            email=email,
            client_uuid=client_uuid,
            encryption_key_fingerprint=fingerprint,
            source_ip=source_ip,
            user_agent=user_agent,
        )
    except Exception:
        logger.exception("Failed to log enroll call")

    return stored


def _enqueue_enroll_payload(
    *,
    body: bytes,
    email: str,
    client_uuid: str,
    fingerprint: str,
    device_name: str,
    source_ip: str | None,
    user_agent: str | None,
    dedupe_key: str | None = None,
) -> tuple[bool, str | None]:
    queue = _get_queue_manager()
    if not queue:
        return False, None
    payload = {
        "body_b64": _b64url_encode(body),
        "email": email,
        "client_uuid": client_uuid,
        "fingerprint": fingerprint,
        "device_name": device_name,
        "source_ip": source_ip or "",
        "user_agent": user_agent or "",
        "received_at": int(time.time()),
    }
    try:
        job_id, _status = queue.enqueue(
            topic="enroll.process",
            payload=payload,
            dedupe_key=dedupe_key,
        )
        return True, job_id
    except Exception:
        logger.exception("Failed to enqueue enroll payload; falling back to sync processing")
        return False, None


def _decode_job_body(encoded: str) -> bytes:
    value = str(encoded or "").strip()
    if not value:
        return b""
    return _b64url_decode(value)


def _persist_telemetry_spans(body: bytes, client_uuid: str) -> None:
    """Parse OTLP JSON payload and insert spans into device_telemetry_events."""
    dsn = _db_url_bootstrap() or _db_url()
    if not dsn or psycopg2 is None:
        return
    try:
        otlp = json.loads(body)
    except Exception:
        return
    rows: list[tuple] = []
    for rs in otlp.get("resourceSpans", []):
        res_attrs = {a["key"]: a.get("value", {}).get("stringValue", "") for a in rs.get("resource", {}).get("attributes", [])}
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                name = span.get("name", "")
                if not name:
                    continue
                span_attrs = {a["key"]: a.get("value", {}).get("stringValue", "") for a in span.get("attributes", [])}
                email = span_attrs.get("user.email") or res_attrs.get("user.email") or ""
                plugin_version = span_attrs.get("extension.version") or res_attrs.get("service.version") or ""
                start_ns = int(span.get("startTimeUnixNano", 0))
                span_ts = datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc) if start_ns else None
                rows.append((client_uuid, email, name, span_ts, json.dumps(span_attrs), plugin_version))
    if not rows:
        return
    try:
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO device_telemetry_events (client_uuid, email, span_name, span_ts, attributes, plugin_version) VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                    rows,
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to persist telemetry spans to DB")


def _process_queue_job(job: QueueJob) -> None:
    if job.topic == "telemetry.forward":
        payload = job.payload if isinstance(job.payload, dict) else {}
        body = _decode_job_body(str(payload.get("body_b64") or ""))
        content_type = str(payload.get("content_type") or "application/json")
        user_agent = str(payload.get("user_agent") or "").strip() or None
        client_uuid = str(payload.get("client_uuid") or "").strip()
        response = _forward_telemetry_to_upstream(
            body,
            content_type=content_type,
            user_agent=user_agent,
        )
        status = int(getattr(response, "status_code", 500) or 500)
        if status < 200 or status >= 300:
            raise RuntimeError(f"telemetry upstream returned status={status}")
        _persist_telemetry_spans(body, client_uuid)
        return
    if job.topic == "enroll.process":
        payload = job.payload if isinstance(job.payload, dict) else {}
        body = _decode_job_body(str(payload.get("body_b64") or ""))
        email = str(payload.get("email") or "").strip()
        client_uuid = str(payload.get("client_uuid") or "").strip()
        fingerprint = str(payload.get("fingerprint") or "unknown").strip() or "unknown"
        device_name = str(payload.get("device_name") or "").strip()
        source_ip = str(payload.get("source_ip") or "").strip() or None
        user_agent = str(payload.get("user_agent") or "").strip() or None
        if not email or not client_uuid or not device_name:
            raise RuntimeError("invalid enroll.process payload")
        _persist_enroll_side_effects(
            body=body,
            email=email,
            client_uuid=client_uuid,
            fingerprint=fingerprint,
            device_name=device_name,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return
    raise RuntimeError(f"unknown queue topic '{job.topic}'")


def _run_queue_worker_loop(stop_event: threading.Event | None = None, once: bool = False) -> None:
    queue = _get_queue_manager()
    if not queue:
        logger.warning("Queue worker started but queue is disabled/unavailable.")
        return
    worker_id = _queue_worker_id("worker")
    stop = stop_event or threading.Event()
    poll_interval = max(0.1, float(settings.queue_poll_interval_seconds))
    batch_size = max(1, int(settings.queue_batch_size))
    logger.info("Queue worker loop started: worker_id=%s batch_size=%s", worker_id, batch_size)

    while not stop.is_set():
        jobs = queue.claim_jobs(worker_id=worker_id, limit=batch_size)
        if not jobs:
            if once:
                return
            stop.wait(poll_interval)
            continue

        for job in jobs:
            try:
                _process_queue_job(job)
                queue.ack(job_id=job.id, worker_id=worker_id)
            except Exception as exc:
                error_text = str(exc or "job processing failed")
                if int(job.attempts) >= int(job.max_attempts):
                    queue.move_to_dead_letter(job=job, worker_id=worker_id, error_text=error_text)
                else:
                    queue.retry(
                        job_id=job.id,
                        worker_id=worker_id,
                        attempts=job.attempts,
                        error_text=error_text,
                    )
                logger.warning(
                    "Queue job failed: worker_id=%s topic=%s job_id=%s attempts=%s/%s error=%s",
                    worker_id,
                    job.topic,
                    job.id,
                    job.attempts,
                    job.max_attempts,
                    error_text,
                )


def _s3_connectivity_check_worker() -> None:
    s3_required = settings.store_enroll_s3 or settings.binaries_mode in ("presign", "proxy")
    if not s3_required:
        logger.info("S3 startup check skipped: S3 is not required by current settings.")
        return
    if not settings.s3_bucket:
        logger.warning("S3 startup check skipped: DM_S3_BUCKET is not set.")
        return

    logger.info("S3 startup check: testing connectivity to bucket '%s'...", settings.s3_bucket)
    try:
        endpoint_url = settings.s3_endpoint_url or None
        region = settings.aws_region or os.getenv("AWS_REGION") or None
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                connect_timeout=2,
                read_timeout=3,
                retries={"max_attempts": 1},
            ),
        )
        s3.head_bucket(Bucket=settings.s3_bucket)
        logger.info("S3 startup check: connectivity OK for bucket '%s'.", settings.s3_bucket)
    except Exception as exc:
        logger.warning("S3 startup check failed (non-blocking): %r", exc)


def _start_s3_connectivity_check_non_blocking() -> None:
    threading.Thread(target=_s3_connectivity_check_worker, daemon=True).start()


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
    elif s3_required and settings.s3_bucket:
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


@app.get("/livez")
def livez():
    """Lightweight liveness endpoint (no external dependency checks)."""
    return JSONResponse(
        status_code=200,
        media_type="application/problem+json",
        content={
            "type": "https://example.com/problems/liveness",
            "title": "OK",
            "status": 200,
            "detail": "Process is alive.",
        },
    )


@app.get("/ops/queue/health")
def queue_health(request: Request):
    _queue_admin_guard(request)
    queue = _get_queue_manager()
    if not settings.queue_enabled:
        return JSONResponse(status_code=200, content={"ok": True, "queue": {"enabled": False, "status": "disabled"}})
    if not queue:
        return JSONResponse(status_code=503, content={"ok": False, "queue": {"enabled": True, "status": "unavailable"}})
    stats = queue.stats()
    healthy = int(stats.get("stale_processing", 0)) == 0
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "ok": healthy,
            "queue": {
                "enabled": True,
                "status": "ok" if healthy else "degraded",
                "stats": stats,
            },
        },
    )


@app.get("/ops/queue/stats")
def queue_stats(request: Request):
    _queue_admin_guard(request)
    queue = _get_queue_manager()
    if not settings.queue_enabled:
        return JSONResponse(status_code=200, content={"ok": True, "queue": {"enabled": False, "stats": {}}})
    if not queue:
        return JSONResponse(status_code=503, content={"ok": False, "error": "queue is unavailable"})
    return JSONResponse(status_code=200, content={"ok": True, "queue": {"enabled": True, "stats": queue.stats()}})


@app.get("/metrics")
def metrics():
    if not bool(settings.metrics_enabled):
        raise HTTPException(status_code=404, detail="Metrics endpoint is disabled.")

    queue_enabled = bool(settings.queue_enabled)
    queue_available = 0
    scrape_ok = 1
    stats: dict[str, int] = {
        "pending": 0,
        "processing": 0,
        "done": 0,
        "dead": 0,
        "total": 0,
        "oldest_pending_age_seconds": 0,
        "stale_processing": 0,
    }

    if queue_enabled:
        queue = _get_queue_manager()
        if queue:
            queue_available = 1
            try:
                raw_stats = queue.stats()
                stats = {
                    "pending": int(raw_stats.get("pending", 0) or 0),
                    "processing": int(raw_stats.get("processing", 0) or 0),
                    "done": int(raw_stats.get("done", 0) or 0),
                    "dead": int(raw_stats.get("dead", 0) or 0),
                    "total": int(raw_stats.get("total", 0) or 0),
                    "oldest_pending_age_seconds": int(raw_stats.get("oldest_pending_age_seconds", 0) or 0),
                    "stale_processing": int(raw_stats.get("stale_processing", 0) or 0),
                }
            except Exception:
                queue_available = 0
                scrape_ok = 0
                logger.exception("Failed to read queue stats for /metrics")
        else:
            scrape_ok = 0

    mode = str(settings.runtime_mode or "api").strip().lower()
    worker_active = 1 if mode in ("worker", "all") else 0
    lines = [
        "# HELP dm_metrics_scrape_success 1 if the metrics collection succeeded.",
        "# TYPE dm_metrics_scrape_success gauge",
        f"dm_metrics_scrape_success {scrape_ok}",
        "# HELP dm_queue_enabled 1 if Postgres queue is enabled.",
        "# TYPE dm_queue_enabled gauge",
        f"dm_queue_enabled {1 if queue_enabled else 0}",
        "# HELP dm_queue_available 1 if queue backend is reachable.",
        "# TYPE dm_queue_available gauge",
        f"dm_queue_available {queue_available}",
        "# HELP dm_queue_pending_jobs Number of pending jobs.",
        "# TYPE dm_queue_pending_jobs gauge",
        f"dm_queue_pending_jobs {stats['pending']}",
        "# HELP dm_queue_processing_jobs Number of processing jobs.",
        "# TYPE dm_queue_processing_jobs gauge",
        f"dm_queue_processing_jobs {stats['processing']}",
        "# HELP dm_queue_done_jobs Number of completed jobs.",
        "# TYPE dm_queue_done_jobs counter",
        f"dm_queue_done_jobs {stats['done']}",
        "# HELP dm_queue_dead_jobs Number of dead jobs.",
        "# TYPE dm_queue_dead_jobs gauge",
        f"dm_queue_dead_jobs {stats['dead']}",
        "# HELP dm_queue_total_jobs Total jobs tracked in queue table.",
        "# TYPE dm_queue_total_jobs gauge",
        f"dm_queue_total_jobs {stats['total']}",
        "# HELP dm_queue_oldest_pending_age_seconds Age in seconds of the oldest pending job.",
        "# TYPE dm_queue_oldest_pending_age_seconds gauge",
        f"dm_queue_oldest_pending_age_seconds {stats['oldest_pending_age_seconds']}",
        "# HELP dm_queue_stale_processing_jobs Number of stale processing jobs beyond lock TTL.",
        "# TYPE dm_queue_stale_processing_jobs gauge",
        f"dm_queue_stale_processing_jobs {stats['stale_processing']}",
        "# HELP dm_runtime_worker_active 1 if current process can execute queue jobs.",
        "# TYPE dm_runtime_worker_active gauge",
        f'dm_runtime_worker_active{{mode="{mode}"}} {worker_active}',
    ]

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )



@app.get("/config/config.json")
def get_config(request: Request, profile: str | None = None, device: str | None = None):
    """Return remote-config JSON (EnrichedConfigResponse v2).

    The response is loaded from a static template file under `config/` and supports
    placeholder substitution with environment variables using the syntax: ${{VARNAME}}.

    Profile selection:
    - Request: /config/config.json?profile=dev|prod
    - Default: DM_CONFIG_PROFILE (defaults to "prod")
    """
    prof = (profile or os.getenv("DM_CONFIG_PROFILE", "prod")).strip().lower()
    if not prof or len(prof) > 50:
        return JSONResponse(status_code=400, content={"ok": False, "error": "profile must be 'dev' or 'prod' or 'int' "})
    dev = (device or "").strip().lower()

    # ── P2: Check config cache ──
    # Skip cache when enrichment headers are present (update directive is device-specific)
    _has_enrichment = bool(
        request.headers.get("X-Plugin-Version", "").strip()
        or request.headers.get("X-Client-UUID", "").strip()
        or request.headers.get("X-User-Email", "").strip()
    )
    cache_key = f"{dev or '_'}:{prof}"
    if not _has_enrichment:
        cached = _config_cache_get(cache_key)
        if cached is not None:
            return JSONResponse(cached, headers={"Cache-Control": "public, max-age=60", "X-Cache": "HIT"})

    device_name = dev
    device_type = dev
    plugin_id = None
    resolved_via = "unknown"

    # ── STEPS 1+2: Resolve device + load template (single pooled connection) ──
    pool_ctx = _pooled_conn()
    if dev and pool_ctx is not None:
        try:
            with pool_ctx as pconn:
                with pconn.cursor() as rcur:
                    device_name, device_type, plugin_id, resolved_via = _resolve_device(dev, rcur)
                    if not device_name:
                        return JSONResponse(status_code=400, content={"ok": False, "error": "device inconnu"})
                    if resolved_via == "alias" and plugin_id:
                        client_uuid_hdr = request.headers.get("X-Client-UUID", "")
                        _log_alias_access(rcur, alias=dev, slug=device_name,
                                          plugin_id=plugin_id, client_uuid=client_uuid_hdr,
                                          source_ip=request.client.host if request.client else None)
                    # Step 2: load template in same connection
                    try:
                        cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None, cur=rcur)
                    except FileNotFoundError as e:
                        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        except Exception:
            if dev in _DEVICE_TYPE_FALLBACK:
                device_name, device_type, plugin_id, resolved_via = dev, dev, None, "fallback"
                try:
                    cfg = _load_config_template(prof, device=device_type, device_name=device_name)
                except FileNotFoundError as e:
                    return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
            else:
                return JSONResponse(status_code=400, content={"ok": False, "error": "device inconnu"})
    elif dev:
        # No pool — raw connect fallback
        db_url = _db_url_bootstrap() or _db_url()
        if psycopg2 is not None and db_url:
            try:
                resolve_conn = psycopg2.connect(db_url)
                resolve_conn.autocommit = True
                with resolve_conn.cursor() as rcur:
                    device_name, device_type, plugin_id, resolved_via = _resolve_device(dev, rcur)
                    if not device_name:
                        resolve_conn.close()
                        return JSONResponse(status_code=400, content={"ok": False, "error": "device inconnu"})
                    if resolved_via == "alias" and plugin_id:
                        client_uuid_hdr = request.headers.get("X-Client-UUID", "")
                        _log_alias_access(rcur, alias=dev, slug=device_name,
                                          plugin_id=plugin_id, client_uuid=client_uuid_hdr,
                                          source_ip=request.client.host if request.client else None)
                    try:
                        cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None, cur=rcur)
                    except FileNotFoundError as e:
                        resolve_conn.close()
                        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
                resolve_conn.close()
            except Exception:
                if dev in _DEVICE_TYPE_FALLBACK:
                    device_name, device_type, plugin_id, resolved_via = dev, dev, None, "fallback"
                else:
                    return JSONResponse(status_code=400, content={"ok": False, "error": "device inconnu"})
                try:
                    cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None)
                except FileNotFoundError as e:
                    return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        else:
            try:
                cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None)
            except FileNotFoundError as e:
                return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    else:
        try:
            cfg = _load_config_template(prof, device=device_type or None, device_name=device_name or None)
        except FileNotFoundError as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    # ── STEP 3: Substitution + DM overrides ──
    cfg = _substitute_env(cfg)
    cfg = _apply_overrides(cfg, profile=prof, device=device_type or None)

    # ── STEPS 4+5: Catalog overrides + access control (pooled connection) ──
    if plugin_id and psycopg2 is not None:
        cat_ctx = _pooled_conn()
        if cat_ctx is not None:
            try:
                with cat_ctx as cconn:
                    with cconn.cursor() as ccur:
                        cfg = _apply_catalog_overrides(cfg, plugin_id=plugin_id, profile=prof, cur=ccur)
                        try:
                            ccur.execute("SELECT id, access_mode, required_group FROM plugins WHERE id = %s", (plugin_id,))
                            plugin_row = None
                            row = ccur.fetchone()
                            if row:
                                plugin_row = {"id": row[0], "access_mode": row[1], "required_group": row[2]}
                            if not _check_plugin_access(plugin_row, request, ccur):
                                return JSONResponse({
                                    "meta": {"schema_version": 2, "access_denied": True},
                                    "config": {
                                        "device_name": device_name,
                                        "access_mode": plugin_row.get("access_mode") if plugin_row else "open",
                                        "message": "Acces restreint. Contactez votre administrateur.",
                                    }
                                })
                        except Exception:
                            pass
            except Exception:
                pass
        else:
            db_url = _db_url_bootstrap() or _db_url()
            if db_url:
                try:
                    cat_conn = psycopg2.connect(db_url)
                    cat_conn.autocommit = True
                    with cat_conn.cursor() as ccur:
                        cfg = _apply_catalog_overrides(cfg, plugin_id=plugin_id, profile=prof, cur=ccur)
                        try:
                            ccur.execute("SELECT id, access_mode, required_group FROM plugins WHERE id = %s", (plugin_id,))
                            plugin_row = None
                            row = ccur.fetchone()
                            if row:
                                plugin_row = {"id": row[0], "access_mode": row[1], "required_group": row[2]}
                            if not _check_plugin_access(plugin_row, request, ccur):
                                cat_conn.close()
                                return JSONResponse({
                                    "meta": {"schema_version": 2, "access_denied": True},
                                    "config": {
                                        "device_name": device_name,
                                        "access_mode": plugin_row.get("access_mode") if plugin_row else "open",
                                        "message": "Acces restreint. Contactez votre administrateur.",
                                    }
                                })
                        except Exception:
                            pass
                    cat_conn.close()
                except Exception:
                    pass

    # ── STEP 6: Inject real device_name + config_path ──
    config_obj = cfg.get("config")
    if isinstance(config_obj, dict) and device_name:
        config_obj["device_name"] = device_name
        config_obj["config_path"] = f"/config/{device_name}/config.json"

    # ── Relay auth + scrub secrets ──
    relay_ok, relay_meta = _relay_auth_from_request(request, target="config")
    if settings.relay_require_key_for_secrets and not relay_ok:
        cfg = _scrub_secret_values(cfg)

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

    # ---- Step 1: Parse new enrichment headers (all optional)
    plugin_version = request.headers.get("X-Plugin-Version", "").strip()
    platform_type = request.headers.get("X-Platform-Type", dev).strip().lower()
    platform_version = request.headers.get("X-Platform-Version", "").strip()
    manifest_version_str = request.headers.get("X-Manifest-Version", "").strip()
    manifest_version: int | None = int(manifest_version_str) if manifest_version_str.isdigit() else None
    client_uuid = request.headers.get("X-Client-UUID", "").strip()
    email = request.headers.get("X-User-Email", "").strip()

    # ---- Step 2: Infer platform_variant
    platform_variant = _infer_platform_variant(platform_type, platform_version, manifest_version)

    # ---- Steps 3-9: DB-backed enrichment (pooled connection, degrade gracefully)
    update_directive: dict | None = None
    flags: dict = {}

    enrich_ctx = _pooled_conn()
    if enrich_ctx is not None:
        try:
            with enrich_ctx as econn:
                with econn.cursor() as cur:
                    device_cohort_ids = _resolve_device_cohorts(
                        cur, email=email, client_uuid=client_uuid
                    )
                    flags = _resolve_feature_flags(
                        cur,
                        device_cohort_ids=device_cohort_ids,
                        plugin_version=plugin_version,
                    )
                    campaign = _resolve_active_campaign(
                        cur,
                        device_cohort_ids=device_cohort_ids,
                        device_type=device_type or "misc",
                        platform_version=platform_version,
                    )
                    update_directive = _build_update_directive(
                        plugin_version=plugin_version,
                        campaign=campaign,
                        client_uuid=client_uuid,
                        device_name=device_name or "",
                    )
                    if update_directive is not None and campaign and client_uuid:
                        try:
                            _upsert_campaign_device_status(
                                cur,
                                campaign_id=campaign["campaign_id"],
                                client_uuid=client_uuid,
                                email=email,
                                version_before=plugin_version,
                            )
                        except Exception:
                            logger.debug("campaign_device_status upsert skipped (table may not exist)")
        except Exception:
            update_directive = None
            flags = {}
    elif psycopg2 is not None:
        # Fallback: raw connection if pool unavailable
        db_url = _db_url_bootstrap() or _db_url()
        if db_url:
            try:
                conn = psycopg2.connect(db_url)
                conn.autocommit = True
                try:
                    with conn.cursor() as cur:
                        device_cohort_ids = _resolve_device_cohorts(
                            cur, email=email, client_uuid=client_uuid
                        )
                        flags = _resolve_feature_flags(
                            cur,
                            device_cohort_ids=device_cohort_ids,
                            plugin_version=plugin_version,
                        )
                        campaign = _resolve_active_campaign(
                            cur,
                            device_cohort_ids=device_cohort_ids,
                            device_type=device_type or "misc",
                            platform_version=platform_version,
                        )
                        update_directive = _build_update_directive(
                            plugin_version=plugin_version,
                            campaign=campaign,
                            client_uuid=client_uuid,
                            device_name=device_name or "",
                        )
                        if update_directive is not None and campaign and client_uuid:
                            try:
                                _upsert_campaign_device_status(
                                    cur,
                                    campaign_id=campaign["campaign_id"],
                                    client_uuid=client_uuid,
                                    email=email,
                                    version_before=plugin_version,
                                )
                            except Exception:
                                logger.debug("campaign_device_status upsert skipped (table may not exist)")
                finally:
                    conn.close()
            except Exception:
                update_directive = None
                flags = {}

    # ---- Step 10: Build final EnrichedConfigResponse
    inner_config = cfg.get("config") if isinstance(cfg.get("config"), dict) else cfg

    response_body = {
        "meta": {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "device_type": device_type or "misc",
            "device_name": device_name or dev or "misc",
            "platform_variant": platform_variant,
            "client_uuid": client_uuid,
            "profile": prof,
        },
        "config": inner_config,
        "update": update_directive,
        "features": flags,
    }

    # P2: Cache the response only when no enrichment headers (generic response)
    if not _has_enrichment:
        _config_cache_set(cache_key, response_body)

    return JSONResponse(response_body, headers={
        "Cache-Control": "public, max-age=60" if not _has_enrichment else "no-store",
        "X-Cache": "MISS",
    })


@app.get("/config/{device}/config.json")
def get_device_config(request: Request, device: str, profile: str | None = None):
    return get_config(request=request, profile=profile, device=device)


@app.post("/config/cache/clear")
def clear_config_cache(request: Request):
    """Flush the config response cache. Call after a deploy or config change.

    Accepts an optional JSON body: {"device": "slug", "profile": "int"}
    to clear only a specific entry, or no body to flush everything.
    """
    _config_cache_clear()
    return JSONResponse({"ok": True, "message": "Config cache cleared"})


@app.get("/telemetry/token")
def get_telemetry_token(profile: str | None = None, device: str | None = None):
    prof = (profile or os.getenv("DM_CONFIG_PROFILE", "prod")).strip().lower()
    dev = (device or "").strip().lower()

    if not settings.telemetry_enabled:
        return JSONResponse(
            status_code=200,
            content={
                "telemetryEnabled": False,
                "telemetryAuthorizationType": settings.telemetry_authorization_type,
                "telemetryKey": "",
            },
        )
    token, exp = _mint_telemetry_token(device=dev or None, profile=prof)
    if settings.telemetry_require_token and not token:
        raise HTTPException(status_code=503, detail="Telemetry signing key is not configured.")
    return JSONResponse(
        status_code=200,
        content={
            "telemetryEnabled": True,
            "telemetryEndpoint": _resolve_public_telemetry_endpoint(),
            "telemetryAuthorizationType": settings.telemetry_authorization_type,
            "telemetryKey": token,
            "telemetryKeyExpiresAt": exp,
            "telemetryKeyTtlSeconds": int(settings.telemetry_token_ttl_seconds),
        },
        headers={"Cache-Control": "no-store"},
    )



@app.get("/relay/authorize")
def relay_authorize(request: Request, target: str | None = None):
    target_name = (target or request.headers.get("x-relay-target") or "").strip().lower()
    ok, info = _relay_auth_from_request(
        request,
        target=target_name,
        require_proxy_token=True,
    )
    if not ok:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(info)})
    meta = info if isinstance(info, dict) else {}
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "target": target_name,
            "client_uuid": meta.get("client_uuid", ""),
            "email": meta.get("email", ""),
            "expires_at": meta.get("expires_at", 0),
        },
    )


@app.get("/relay/introspect")
def relay_introspect(request: Request, target: str | None = None):
    target_name = (target or request.headers.get("x-relay-target") or "").strip().lower()
    ok, info = _relay_auth_from_request(
        request,
        target=target_name,
        require_proxy_token=False,
    )
    if not ok:
        return JSONResponse(status_code=401, content={"ok": False, "error": str(info)})
    return JSONResponse(status_code=200, content={"ok": True, "relay": info})

@app.api_route("/v1/traces", methods=["POST", "OPTIONS"])
@app.api_route("/telemetry/v1/traces", methods=["POST", "OPTIONS"])
async def telemetry_traces(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not settings.telemetry_enabled:
        raise HTTPException(status_code=503, detail="Telemetry relay is disabled.")

    body = await request.body()
    if len(body) == 0:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Empty telemetry payload"})
    if len(body) > TELEMETRY_MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"ok": False, "error": "Telemetry payload too large"})

    if settings.telemetry_require_token:
        token = _extract_bearer_token(request)
        client_uuid = None
        if token:
            try:
                payload = _verify_telemetry_token(token)
                client_uuid = str(payload.get("jti") or "telemetry")
            except HTTPException:
                # Token invalid/expired – fall through to X-Client-UUID fallback
                pass
        if not client_uuid:
            # Fallback: accept X-Client-UUID header for pre-enrollment devices
            # or when the Bearer token has expired.
            header_uuid = (
                request.headers.get("x-client-uuid")
                or request.headers.get("x-plugin-uuid")
                or ""
            ).strip()
            if not header_uuid:
                raise HTTPException(
                    status_code=401,
                    detail="Missing telemetry Bearer token or X-Client-UUID header.",
                )
            client_uuid = _normalize_client_uuid(header_uuid)
    else:
        client_uuid = "telemetry-open"

    try:
        _log_device_connection(
            action="TELEMETRY_RELAY",
            email="telemetry@local",
            client_uuid=client_uuid,
            encryption_key_fingerprint="none",
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception("Failed to log telemetry relay call")

    content_type = request.headers.get("content-type", "application/json")
    user_agent = request.headers.get("user-agent")
    idempotency_key = (
        request.headers.get("x-idempotency-key")
        or request.headers.get("x-request-id")
        or ""
    ).strip()
    dedupe_key = f"telemetry:{client_uuid}:{idempotency_key}" if idempotency_key else None

    queued, job_id = _enqueue_telemetry_payload(
        body=body,
        content_type=content_type,
        user_agent=user_agent,
        client_uuid=client_uuid,
        dedupe_key=dedupe_key,
    )
    if queued:
        return JSONResponse(
            status_code=202,
            content={"ok": True, "queued": True, "jobId": job_id},
        )

    return _forward_telemetry_to_upstream(
        body,
        content_type=content_type,
        user_agent=user_agent,
    )


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
    if not isinstance(body_obj, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Body must be a JSON object"})

    missing = _validate_enroll_payload(body_obj)
    if missing:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "Missing required fields: " + ", ".join(missing),
            },
        )

    access_token = _extract_access_token_from_request(request)
    try:
        auth_email = _email_from_access_token(access_token)
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        auth_email = ""
    if not auth_email:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "Missing or invalid PKCE access token."},
        )

    device_name = str(body_obj.get("device_name", "")).strip()
    plugin_uuid = str(body_obj.get("plugin_uuid", "")).strip()
    email = auth_email
    _email, client_uuid, fingerprint = _extract_identity(request, body_obj=body_obj)
    if plugin_uuid:
        client_uuid = plugin_uuid
    client_uuid = _normalize_client_uuid(client_uuid)
    source_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    relay_data = _mint_or_rotate_relay_credentials(client_uuid=client_uuid, email=email)

    idempotency_key = (
        request.headers.get("x-idempotency-key")
        or request.headers.get("x-request-id")
        or ""
    ).strip()
    dedupe_key = f"enroll:{client_uuid}:{idempotency_key}" if idempotency_key else None

    queued, job_id = _enqueue_enroll_payload(
        body=body,
        email=email,
        client_uuid=client_uuid,
        fingerprint=fingerprint,
        device_name=device_name,
        source_ip=source_ip,
        user_agent=user_agent,
        dedupe_key=dedupe_key,
    )

    stored: dict[str, str | bool] = {}
    if queued:
        stored = {"queued": True, "jobId": str(job_id or "")}
    else:
        try:
            stored = _persist_enroll_side_effects(
                body=body,
                email=email,
                client_uuid=client_uuid,
                fingerprint=fingerprint,
                device_name=device_name,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot persist enroll payload: {e!r}")

    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "stored": stored,
            "queued": queued,
            "jobId": job_id,
            "relay": relay_data,
            "relayClientId": relay_data.get("client_id", ""),
            "relayClientKey": relay_data.get("client_key", ""),
            "relayKeyExpiresAt": relay_data.get("expires_at", 0),
        },
    )


# ── Update status reporting ─────────────────────────────────────────────


@app.post("/update/status")
async def report_update_status(request: Request):
    """Receive update status report from a plugin after install/failure."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    campaign_id = body.get("campaign_id")
    client_uuid = body.get("client_uuid", "")
    status = body.get("status", "")
    version_before = body.get("version_before", "")
    version_after = body.get("version_after", "")
    error_detail = body.get("error_detail", "")

    allowed = ("installed", "failed", "checksum_error", "download_error", "deferred")
    if status not in allowed:
        return JSONResponse({"ok": False, "error": f"status must be one of {allowed}"}, status_code=400)
    if not client_uuid:
        return JSONResponse({"ok": False, "error": "client_uuid required"}, status_code=400)

    db_url = _db_url()
    if db_url:
        try:
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
            with conn.cursor() as cur:
                # Map plugin status to DB enum
                db_status = "updated" if status == "installed" else "failed"
                cur.execute(
                    """
                    INSERT INTO campaign_device_status
                        (campaign_id, client_uuid, status, version_before, version_after, error_message, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (campaign_id, client_uuid)
                    DO UPDATE SET status = EXCLUDED.status,
                                  version_after = EXCLUDED.version_after,
                                  error_message = EXCLUDED.error_message,
                                  updated_at = NOW()
                    """,
                    (campaign_id, client_uuid, db_status,
                     version_before, version_after or None, error_detail or None),
                )
            conn.close()
        except Exception as e:
            logger.warning(f"update/status DB error: {e}")

    return JSONResponse({"ok": True, "status": status})


# ── Campaign REST API ───────────────────────────────────────────────────


def _verify_admin_token(request: Request) -> bool:
    """Check X-Admin-Token header against DM_QUEUE_ADMIN_TOKEN."""
    expected = os.getenv("DM_QUEUE_ADMIN_TOKEN", "")
    if not expected:
        return False
    token = request.headers.get("X-Admin-Token", "")
    return token == expected


@app.post("/api/plugins/{slug}/deploy")
async def api_plugin_deploy(slug: str, request: Request):
    """Unified deploy endpoint: upload binary + create version + create campaign.

    Multipart form fields:
      - binary (file): the plugin package (.oxt, .xpi, .crx)
      - version (str, optional): version string (auto-detected from package if omitted)
      - strategy (str): "immediate" or "canary" (default: "canary")
      - urgency (str): "low", "normal", "critical" (default: "normal")
      - cohort_id (int, optional): target cohort
    """
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    form = await request.form()
    binary = form.get("binary")
    if not binary or not hasattr(binary, "read"):
        return JSONResponse({"ok": False, "error": "binary file required"}, status_code=400)

    data = await binary.read()
    if not data:
        return JSONResponse({"ok": False, "error": "empty file"}, status_code=400)

    version = str(form.get("version", "")).strip()
    strategy = str(form.get("strategy", "canary")).strip()
    urgency = str(form.get("urgency", "normal")).strip()
    cohort_id = form.get("cohort_id")

    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        return JSONResponse({"ok": False, "error": "Database not configured"}, status_code=500)

    import zipfile as _zf
    import io as _io

    # 1. Resolve plugin
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, device_type, name FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
            prow = cur.fetchone()
            if not prow:
                return JSONResponse({"ok": False, "error": f"Plugin '{slug}' not found"}, status_code=404)
            plugin_id, device_type, plugin_name = prow

            # 2. Auto-detect version from package if not provided
            if not version:
                try:
                    with _zf.ZipFile(_io.BytesIO(data)) as zf:
                        if "manifest.json" in zf.namelist():
                            mf = json.loads(zf.read("manifest.json"))
                            version = mf.get("version", "")
                        if not version and "description.xml" in zf.namelist():
                            desc = zf.read("description.xml").decode("utf-8", errors="replace")
                            m = re.search(r'value="(\d+\.\d+(?:\.\d+)*)"', desc)
                            if m:
                                version = m.group(1)
                        if not version:
                            for name in zf.namelist():
                                if name.rsplit("/", 1)[-1].lower() in ("dm-manifest.json", "dm_manifest.json"):
                                    dm = json.loads(zf.read(name).decode("utf-8", errors="replace"))
                                    cl = dm.get("changelog", [])
                                    if cl and isinstance(cl, list):
                                        version = cl[0].get("version", "")
                                    break
                except Exception:
                    pass
            if not version:
                return JSONResponse({"ok": False, "error": "version required (not detected in package)"}, status_code=400)

            # 3. Extract dm-config.json and dm-manifest.json
            deploy_config_template = None
            dm_manifest = None
            try:
                with _zf.ZipFile(_io.BytesIO(data)) as zf:
                    for zname in zf.namelist():
                        basename = zname.rsplit("/", 1)[-1].lower()
                        if basename in ("dm-config.json", "dm_config.json") and not deploy_config_template:
                            raw = zf.read(zname).decode("utf-8", errors="replace")
                            deploy_config_template = json.loads(raw)
                        elif basename in ("dm-manifest.json", "dm_manifest.json") and not dm_manifest:
                            raw = zf.read(zname).decode("utf-8", errors="replace")
                            dm_manifest = json.loads(raw)
            except Exception:
                pass

            # 4. Strip dm metadata from binary, compute checksum, store
            try:
                strip_names = {"dm-config.json", "dm_config.json", "dm-manifest.json", "dm_manifest.json"}
                src = _zf.ZipFile(_io.BytesIO(data))
                buf = _io.BytesIO()
                with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as dst:
                    for item in src.infolist():
                        if item.filename.lower() in strip_names:
                            continue
                        dst.writestr(item, src.read(item.filename))
                data = buf.getvalue()
            except Exception:
                pass

            checksum = "sha256:" + hashlib.sha256(data).hexdigest()
            filename = binary.filename or f"{slug}-{version}.oxt"
            rel_path = f"{device_type}/{version}_{filename}"

            # Store locally (API pod cache)
            _binaries_base = settings.local_binaries_dir
            os.makedirs(os.path.join(_binaries_base, device_type), exist_ok=True)
            full_path = os.path.join(_binaries_base, rel_path)
            with open(full_path, "wb") as f:
                f.write(data)

            # Forward to admin pod (persistent storage) for pull-on-miss
            _admin_url = os.getenv("DM_ADMIN_INTERNAL_URL", "http://device-management-admin").rstrip("/")
            _admin_token = (settings.queue_admin_token or "").strip()
            _admin_base = "/data/content/binaries"
            _admin_full = f"{_admin_base}/{rel_path}"
            if _admin_token:
                try:
                    import io as _io2
                    _files = {"file": (filename, _io2.BytesIO(data), "application/octet-stream")}
                    _resp = httpx.put(
                        f"{_admin_url}/admin/api/files/upload/{rel_path}",
                        files=_files, headers={"x-admin-token": _admin_token}, timeout=30,
                    )
                    if _resp.status_code == 200:
                        logger.info("api_plugin_deploy: forwarded %s to admin pod", rel_path)
                except Exception as fwd_err:
                    logger.warning("api_plugin_deploy: admin forward failed: %s", fwd_err)

            # 5. Upsert artifact — use admin path as canonical (persistent)
            cur.execute("""
                INSERT INTO artifacts (device_type, platform_variant, version, s3_path, checksum)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (device_type, platform_variant, version) DO UPDATE SET
                    s3_path = EXCLUDED.s3_path, checksum = EXCLUDED.checksum, released_at = NOW()
                RETURNING id
            """, (device_type, "", version, _admin_full, checksum))
            artifact_id = cur.fetchone()[0]

            # 6. Deprecate old versions + upsert new version
            cur.execute("UPDATE plugin_versions SET status = 'deprecated' WHERE plugin_id = %s AND status = 'published' AND version <> %s", (plugin_id, version))
            release_notes = ""
            if dm_manifest:
                for entry in dm_manifest.get("changelog", []):
                    if entry.get("version") == version:
                        release_notes = "\n".join(f"- {c}" for c in entry.get("changes", []))
                        break
            cur.execute("""
                INSERT INTO plugin_versions (plugin_id, version, artifact_id, release_notes, status, published_at, distribution_mode)
                VALUES (%s, %s, %s, %s, 'published', NOW(), 'managed')
                ON CONFLICT (plugin_id, version) DO UPDATE SET
                    artifact_id = EXCLUDED.artifact_id,
                    release_notes = CASE WHEN EXCLUDED.release_notes <> '' THEN EXCLUDED.release_notes ELSE plugin_versions.release_notes END,
                    status = 'published',
                    published_at = COALESCE(plugin_versions.published_at, NOW())
                RETURNING id
            """, (plugin_id, version, artifact_id, release_notes))
            version_id = cur.fetchone()[0]

            # 7. Update config_template + changelog from manifest
            if deploy_config_template:
                try:
                    cur.execute("UPDATE plugins SET config_template = %s WHERE id = %s",
                                (json.dumps(deploy_config_template), plugin_id))
                except Exception:
                    pass
            if dm_manifest and dm_manifest.get("changelog"):
                try:
                    cur.execute("UPDATE plugins SET changelog = %s WHERE id = %s",
                                (json.dumps(dm_manifest["changelog"]), plugin_id))
                except Exception:
                    pass

            # 8. Auto-complete old campaigns + create new one
            cur.execute("""
                UPDATE campaigns SET status = 'completed', updated_at = NOW()
                WHERE status IN ('active', 'paused') AND type = 'plugin_update'
                  AND (plugin_id = %s OR plugin_id IS NULL)
            """, (plugin_id,))

            rollout_config = None
            if strategy == "canary":
                rollout_config = {
                    "stages": [
                        {"percent": 5, "duration_hours": 24, "label": "Canary (5%)"},
                        {"percent": 25, "duration_hours": 48, "label": "Early adopters (25%)"},
                        {"percent": 100, "duration_hours": 0, "label": "General availability"},
                    ]
                }

            cur.execute("""
                INSERT INTO campaigns (name, type, artifact_id, plugin_id, version_id,
                    target_cohort_id, urgency, status, rollout_config, created_by)
                VALUES (%s, 'plugin_update', %s, %s, %s, %s, %s, 'active', %s, 'api')
                RETURNING id
            """, (
                f"Release {plugin_name} v{version}",
                artifact_id, plugin_id, version_id,
                int(cohort_id) if cohort_id else None,
                urgency,
                json.dumps(rollout_config) if rollout_config else None,
            ))
            campaign_id = cur.fetchone()[0]

        return JSONResponse({
            "ok": True,
            "plugin_id": plugin_id,
            "version": version,
            "version_id": version_id,
            "artifact_id": artifact_id,
            "campaign_id": campaign_id,
            "checksum": checksum,
            "strategy": strategy,
        }, status_code=201)
    except Exception as e:
        logger.exception("api_plugin_deploy failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        conn.close()


@app.post("/api/campaigns")
async def api_create_campaign(request: Request):
    """Create a new campaign via REST API."""
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    db_url = _db_url_bootstrap() or _db_url()
    if not db_url:
        return JSONResponse({"ok": False, "error": "Database not configured"}, status_code=500)

    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Resolve plugin_id from artifact's device_type
            plugin_id = body.get("plugin_id")
            artifact_id = body.get("artifact_id")
            if not plugin_id and artifact_id:
                cur.execute("""
                    SELECT p.id FROM plugins p
                    JOIN artifacts a ON a.device_type = p.device_type
                    WHERE a.id = %s AND p.status = 'active' LIMIT 1
                """, (artifact_id,))
                prow = cur.fetchone()
                if prow:
                    plugin_id = prow[0]

            status = body.get("status", "draft")

            # Auto-complete older active campaigns
            if status == "active":
                cur.execute("""
                    UPDATE campaigns SET status = 'completed', updated_at = NOW()
                    WHERE status = 'active' AND type = %s
                      AND (%s IS NULL OR plugin_id = %s OR plugin_id IS NULL)
                """, (body.get("type", "plugin_update"), plugin_id, plugin_id))

            cur.execute(
                """
                INSERT INTO campaigns (name, description, type, artifact_id, rollback_artifact_id,
                    target_cohort_id, urgency, status, rollout_config, created_by, plugin_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    body.get("name", "API Campaign"),
                    body.get("description", ""),
                    body.get("type", "plugin_update"),
                    artifact_id,
                    body.get("rollback_artifact_id"),
                    body.get("target_cohort_id"),
                    body.get("urgency", "normal"),
                    status,
                    json.dumps(body["rollout_config"]) if body.get("rollout_config") else None,
                    "api",
                    plugin_id,
                ),
            )
            campaign_id = cur.fetchone()[0]
        conn.close()
        return JSONResponse({"ok": True, "campaign_id": campaign_id}, status_code=201)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.patch("/api/campaigns/{campaign_id}/start")
async def api_start_campaign(campaign_id: int, request: Request):
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return _api_campaign_action(campaign_id, "active")


@app.patch("/api/campaigns/{campaign_id}/pause")
async def api_pause_campaign(campaign_id: int, request: Request):
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return _api_campaign_action(campaign_id, "paused")


@app.patch("/api/campaigns/{campaign_id}/resume")
async def api_resume_campaign(campaign_id: int, request: Request):
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return _api_campaign_action(campaign_id, "active")


@app.patch("/api/campaigns/{campaign_id}/abort")
async def api_abort_campaign(campaign_id: int, request: Request):
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return _api_campaign_action(campaign_id, "rolled_back")


def _api_campaign_action(campaign_id: int, new_status: str):
    db_url = _db_url()
    if not db_url:
        return JSONResponse({"ok": False, "error": "Database not configured"}, status_code=500)
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE campaigns SET status = %s, updated_at = NOW() WHERE id = %s RETURNING id",
                (new_status, campaign_id),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return JSONResponse({"ok": False, "error": "Campaign not found"}, status_code=404)
        return JSONResponse({"ok": True, "campaign_id": campaign_id, "status": new_status})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/campaigns/{campaign_id}/progress")
async def api_campaign_progress(campaign_id: int, request: Request):
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    db_url = _db_url()
    if not db_url:
        return JSONResponse({"ok": False, "error": "Database not configured"}, status_code=500)

    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT status, rollout_config FROM campaigns WHERE id = %s", (campaign_id,))
            camp_row = cur.fetchone()
            if not camp_row:
                conn.close()
                return JSONResponse({"ok": False, "error": "Campaign not found"}, status_code=404)

            camp_status, rollout_config = camp_row

            cur.execute(
                """
                SELECT status, COUNT(*) FROM campaign_device_status
                WHERE campaign_id = %s GROUP BY status
                """,
                (campaign_id,),
            )
            stats = dict(cur.fetchall())
        conn.close()

        total = sum(stats.values())
        installed = stats.get("updated", 0)
        failed = stats.get("failed", 0)
        notified = stats.get("notified", 0)
        pending = stats.get("pending", 0)

        failure_rate = round(failed / max(installed + failed, 1), 4)

        # Determine current stage label
        current_stage = "unknown"
        if isinstance(rollout_config, dict):
            stages = rollout_config.get("stages", [])
            current_percent = _get_current_rollout_percent({"campaign_created_at": None, "rollout_config": rollout_config}, stages)
            for s in stages:
                if s.get("percent", 100) >= current_percent:
                    current_stage = s.get("label", "unknown")
                    break

        return JSONResponse({
            "ok": True,
            "campaign_id": campaign_id,
            "status": camp_status,
            "current_stage": current_stage,
            "total_devices": total,
            "installed": installed,
            "failed": failed,
            "notified": notified,
            "pending": pending,
            "failure_rate": failure_rate,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/artifacts")
async def api_upload_artifact(request: Request):
    """Upload an artifact binary via REST API."""
    if not _verify_admin_token(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    form = await request.form()
    device_type = form.get("device_type", "libreoffice")
    version = form.get("version", "")
    changelog_url = form.get("changelog_url", "")
    binary = form.get("binary")

    if not version or not binary:
        return JSONResponse({"ok": False, "error": "version and binary required"}, status_code=400)

    data = await binary.read()
    checksum = "sha256:" + hashlib.sha256(data).hexdigest()
    filename = binary.filename or f"mirai-{version}.oxt"

    # Save locally
    _binaries_base = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    binaries_dir = os.path.join(_binaries_base, device_type)
    os.makedirs(binaries_dir, exist_ok=True)
    local_path = os.path.join(device_type, f"{version}_{filename}")
    full_path = os.path.join(_binaries_base, local_path)
    with open(full_path, "wb") as f:
        f.write(data)

    db_url = _db_url()
    if not db_url:
        return JSONResponse({"ok": False, "error": "Database not configured"}, status_code=500)

    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifacts (device_type, version, s3_path, checksum, changelog_url)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (device_type, version, local_path, checksum, changelog_url),
            )
            artifact_id = cur.fetchone()[0]
        conn.close()
        return JSONResponse({"ok": True, "artifact_id": artifact_id, "checksum": checksum}, status_code=201)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/catalog/icons/{filename}")
def catalog_icon(filename: str):
    """Serve plugin icon/logo (public, no auth)."""
    icons_dir = os.getenv("DM_ICONS_DIR", "/data/content/icons")
    safe_name = os.path.basename(filename)
    filepath = os.path.join(icons_dir, safe_name)
    if not os.path.isfile(filepath):
        raise HTTPException(404, "Icon not found")
    return FileResponse(filepath, headers={"Cache-Control": "public, max-age=3600"})


# ─── Public Catalog API ──────────────────────────────────────────────────

@app.get("/catalog/api/plugins")
def api_public_plugins():
    """JSON public — list active plugins for external integration."""
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        return JSONResponse({"plugins": [], "total": 0}, headers={"Access-Control-Allow-Origin": "*"})
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.slug, p.name, p.intent, p.device_type, p.category, p.publisher,
                       p.maturity, p.access_mode, p.icon_url, p.icon_path, p.key_features, p.source_url,
                       COUNT(DISTINCT pi.client_uuid) FILTER (WHERE pi.status='active') AS install_count,
                       MAX(pv.version) FILTER (WHERE pv.status='published') AS latest_version
                FROM plugins p
                LEFT JOIN plugin_installations pi ON pi.plugin_id = p.id
                LEFT JOIN plugin_versions pv ON pv.plugin_id = p.id
                WHERE p.status = 'active' AND p.visibility IN ('public','internal')
                GROUP BY p.id ORDER BY p.name
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        maturity_labels = {"dev":"Dev","alpha":"Alpha","beta":"Beta","pre-release":"Pre-release","release":"Stable"}
        plugins = []
        for p in rows:
            kf = p.get("key_features") or []
            if isinstance(kf, str):
                kf = json.loads(kf)
            # Icon: serve via dedicated endpoint with extension
            _raw_icon = p.get("icon_url") or ""
            if _raw_icon.startswith("data:"):
                _icon_mime = _raw_icon.split(":")[1].split(";")[0] if ":" in _raw_icon else ""
                _icon_ext = _MIME_TO_EXT.get(_icon_mime, "png")
                icon = f"{public_base}/catalog/api/plugins/{p['slug']}/icon.{_icon_ext}"
            elif _raw_icon or p.get("icon_path"):
                icon = f"{public_base}/catalog/api/plugins/{p['slug']}/icon"
            else:
                icon = None
            plugins.append({
                "slug": p["slug"], "name": p["name"], "intent": p.get("intent") or "",
                "device_type": p["device_type"], "category": p.get("category") or "",
                "publisher": p.get("publisher") or "DNUM",
                "maturity": p.get("maturity") or "release",
                "maturity_label": maturity_labels.get(p.get("maturity"), "Stable"),
                "access_mode": p.get("access_mode") or "open",
                "icon_url": icon or None,
                "latest_version": p.get("latest_version"),
                "install_count": p.get("install_count") or 0,
                "key_features": kf,
                "source_url": p.get("source_url"),
                "detail_url": f"{public_base}/catalog/{p['slug']}",
                "download_url": f"{public_base}/catalog/{p['slug']}/download",
            })
        return JSONResponse(
            {"plugins": plugins, "total": len(plugins),
             "generated_at": datetime.now(timezone.utc).isoformat()},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"},
        )
    finally:
        conn.close()


@app.get("/catalog/api/plugins/{slug}")
def api_public_plugin_detail(slug: str):
    """JSON public — plugin detail for external integration."""
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        raise HTTPException(404)
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404)
            cols = [d[0] for d in cur.description]
            p = dict(zip(cols, row))
            # Latest version
            cur.execute("""
                SELECT version, release_notes FROM plugin_versions
                WHERE plugin_id = %s AND status = 'published'
                ORDER BY published_at DESC LIMIT 1
            """, (p["id"],))
            vrow = cur.fetchone()
            cur.execute("SELECT COUNT(DISTINCT client_uuid) FROM plugin_installations WHERE plugin_id=%s AND status='active'", (p["id"],))
            installs = cur.fetchone()[0]
        kf = p.get("key_features") or []
        if isinstance(kf, str):
            kf = json.loads(kf)
        maturity_labels = {"dev":"Dev","alpha":"Alpha","beta":"Beta","pre-release":"Pre-release","release":"Stable"}
        # Icon: serve via dedicated endpoint with extension
        _raw_icon = p.get("icon_url") or ""
        if _raw_icon.startswith("data:"):
            _icon_mime = _raw_icon.split(":")[1].split(";")[0] if ":" in _raw_icon else ""
            _icon_ext = _MIME_TO_EXT.get(_icon_mime, "png")
            icon = f"{public_base}/catalog/api/plugins/{p['slug']}/icon.{_icon_ext}"
        elif _raw_icon or p.get("icon_path"):
            icon = f"{public_base}/catalog/api/plugins/{p['slug']}/icon"
        else:
            icon = None
        return JSONResponse({
            "slug": p["slug"], "name": p["name"], "description": p.get("description") or "",
            "intent": p.get("intent") or "", "device_type": p["device_type"],
            "category": p.get("category") or "", "publisher": p.get("publisher") or "DNUM",
            "maturity": p.get("maturity") or "release",
            "maturity_label": maturity_labels.get(p.get("maturity"), "Stable"),
            "access_mode": p.get("access_mode") or "open",
            "icon_url": icon or None,
            "latest_version": vrow[0] if vrow else None,
            "changelog_summary": (vrow[1] or "")[:200] if vrow else "",
            "install_count": installs,
            "key_features": kf, "source_url": p.get("source_url"),
            "homepage_url": p.get("homepage_url"), "support_email": p.get("support_email"),
            "doc_url": p.get("doc_url"),
            "license": p.get("license"),
            "detail_url": f"{public_base}/catalog/{p['slug']}",
            "download_url": f"{public_base}/catalog/{p['slug']}/download",
        }, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"})
    finally:
        conn.close()


_MIME_TO_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg", "image/webp": "webp"}
_EXT_TO_MIME = {v: k for k, v in _MIME_TO_EXT.items()}


def _resolve_plugin_icon(slug: str) -> tuple[bytes, str] | None:
    """Return (raw_bytes, mime) for a plugin icon, or None."""
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        return None
    icon_url = None
    pool_ctx = _pooled_conn()
    if pool_ctx is not None:
        try:
            with pool_ctx as pconn:
                with pconn.cursor() as cur:
                    cur.execute("SELECT icon_url FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
                    row = cur.fetchone()
                    if row:
                        icon_url = row[0]
        except Exception:
            pass
    else:
        try:
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT icon_url FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
                row = cur.fetchone()
                if row:
                    icon_url = row[0]
            conn.close()
        except Exception:
            pass
    if not icon_url or not icon_url.startswith("data:"):
        return None
    try:
        header, b64data = icon_url.split(",", 1)
        mime = header.split(":")[1].split(";")[0]
        raw = base64.b64decode(b64data)
        return raw, mime
    except Exception:
        return None


@app.get("/catalog/api/plugins/{slug}/icon.{ext}")
def api_public_plugin_icon_ext(slug: str, ext: str):
    """Serve plugin icon with explicit extension (icon.png, icon.jpg, etc.)."""
    result = _resolve_plugin_icon(slug)
    if not result:
        raise HTTPException(404, "Icon not found")
    raw, mime = result
    return Response(content=raw, media_type=_EXT_TO_MIME.get(ext, mime),
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/catalog/api/plugins/{slug}/icon")
def api_public_plugin_icon(slug: str):
    """Serve plugin icon (redirects to versioned URL with extension)."""
    result = _resolve_plugin_icon(slug)
    if not result:
        raise HTTPException(404, "Icon not found")
    _, mime = result
    ext = _MIME_TO_EXT.get(mime, "png")
    return RedirectResponse(f"/catalog/api/plugins/{slug}/icon.{ext}",
                            status_code=301,
                            headers={"Cache-Control": "public, max-age=86400"})


@app.get("/catalog/api/status")
def api_catalog_status():
    """Public status endpoint for catalog availability banner."""
    import urllib.request as urlreq
    services = {}
    for name, check_fn in [
        ("api", lambda: "ok"),
        ("database", lambda: (psycopg2.connect(_db_url_bootstrap() or _db_url()).close() or "ok") if psycopg2 else "skip"),
    ]:
        try:
            check_fn()
            services[name] = "ok"
        except Exception:
            services[name] = "error"
    all_ok = all(v == "ok" for v in services.values())
    return JSONResponse({
        "status": "ok" if all_ok else "degraded",
        "status_label": "Tous les services sont operationnels" if all_ok else "Service degrade",
        "services": services,
    }, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=30"})


# ─── Monitoring Endpoints ────────────────────────────────────────────────

@app.get("/ops/health/full")
def ops_health_full():
    """Detailed health for Grafana/alerting."""
    import urllib.request as urlreq
    import time as _time

    checks = {}
    critical_svcs = {"postgres"}

    def _do(name, fn):
        t0 = _time.monotonic()
        try:
            detail = fn()
            checks[name] = {"status": "ok", "latency_ms": round((_time.monotonic()-t0)*1000), "detail": detail}
        except Exception as e:
            checks[name] = {"status": "error", "latency_ms": round((_time.monotonic()-t0)*1000), "detail": str(e)[:100]}

    db_url = _db_url_bootstrap() or _db_url()
    if psycopg2 and db_url:
        def _db():
            c = psycopg2.connect(db_url); c.cursor().execute("SELECT 1"); c.close(); return "ok"
        _do("postgres", _db)

    issuer = os.getenv("KEYCLOAK_ISSUER_URL", "")
    if issuer:
        _do("keycloak", lambda: (urlreq.urlopen(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=5).close() or "ok"))

    llm_url = os.getenv("LLM_BASE_URL", "")
    if llm_url:
        _do("llm", lambda: (urlreq.urlopen(urlreq.Request(f"{llm_url.rstrip('/')}/models",
             headers={"Authorization": f"Bearer {os.getenv('LLM_API_TOKEN','')}"}), timeout=10).close() or "ok"))

    _do("relay", lambda: (urlreq.urlopen("http://relay-assistant:8080/healthz", timeout=3).close() or "ok"))

    has_critical = any(checks.get(s, {}).get("status") == "error" for s in critical_svcs)
    has_any_err = any(v.get("status") == "error" for v in checks.values())
    global_status = "error" if has_critical else ("degraded" if has_any_err else "ok")

    return JSONResponse({
        "status": global_status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": checks,
    })


@app.get("/ops/metrics")
def ops_metrics():
    """Prometheus text exposition format."""
    lines = []
    # Service checks (reuse health/full logic inline)
    db_url = _db_url_bootstrap() or _db_url()
    db_ok = 0
    if psycopg2 and db_url:
        try:
            c = psycopg2.connect(db_url); c.cursor().execute("SELECT 1"); c.close(); db_ok = 1
        except Exception:
            pass
    lines += ["# HELP dm_service_up Service health (1=ok, 0=error)", "# TYPE dm_service_up gauge",
              f"dm_service_up{{service=\"postgres\"}} {db_ok}"]

    # Business metrics
    if psycopg2 and db_url:
        try:
            conn = psycopg2.connect(db_url); conn.autocommit = True; cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT client_uuid) FROM provisioning WHERE status='ENROLLED'")
            enrolled = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT client_uuid) FROM device_connections WHERE created_at > NOW() - INTERVAL '7 days'")
            active = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM queue_jobs WHERE status='pending'")
            pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM queue_job_dead_letters")
            dead = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM campaigns WHERE status='active'")
            campaigns = cur.fetchone()[0]
            conn.close()
            lines += [
                "# HELP dm_devices_enrolled_total Total enrolled devices", "# TYPE dm_devices_enrolled_total gauge",
                f"dm_devices_enrolled_total {enrolled}",
                "# HELP dm_devices_active_7d Active devices last 7d", "# TYPE dm_devices_active_7d gauge",
                f"dm_devices_active_7d {active}",
                "# HELP dm_queue_pending Pending jobs", "# TYPE dm_queue_pending gauge",
                f"dm_queue_pending {pending}",
                "# HELP dm_queue_dead Dead letters", "# TYPE dm_queue_dead gauge",
                f"dm_queue_dead {dead}",
                "# HELP dm_campaigns_active Active campaigns", "# TYPE dm_campaigns_active gauge",
                f"dm_campaigns_active {campaigns}",
            ]
        except Exception:
            pass

    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@app.options("/catalog/api/{path:path}")
def catalog_api_cors():
    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    })


# ─── Public Catalog HTML Pages ──────────────────────────────────────────

from fastapi.templating import Jinja2Templates as _Jinja2Templates

_catalog_templates = _Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "catalog", "templates")
)

_DEVICE_TYPE_EXT = {"libreoffice": "oxt", "firefox": "xpi", "chrome": "crx", "edge": "crx", "matisse": "xpi"}


@app.get("/catalog", response_class=Response)
def catalog_index(request: Request, category: str | None = None):
    """Public HTML — plugin catalog grid."""
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        return _catalog_templates.TemplateResponse("catalog_index.html", {
            "request": request, "plugins": [], "categories": [], "current_category": None,
        })
    maturity_labels = {"dev": "Dev", "alpha": "Alpha", "beta": "Beta",
                       "pre-release": "Pre-release", "release": "Stable"}
    conn = None
    pool_ctx = _pooled_conn()
    try:
        if pool_ctx is not None:
            conn = pool_ctx.__enter__()
        else:
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.slug, p.name, p.intent, p.device_type, p.category, p.publisher,
                       p.maturity, p.icon_url, p.icon_path, p.key_features,
                       COUNT(DISTINCT pi.client_uuid) FILTER (WHERE pi.status='active') AS install_count,
                       MAX(pv.version) FILTER (WHERE pv.status='published') AS latest_version
                FROM plugins p
                LEFT JOIN plugin_installations pi ON pi.plugin_id = p.id
                LEFT JOIN plugin_versions pv ON pv.plugin_id = p.id
                WHERE p.status = 'active' AND p.visibility IN ('public','internal')
                GROUP BY p.id ORDER BY p.name
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Build category list and filter
        all_categories = sorted({r.get("category") or "" for r in rows} - {""})
        if category:
            rows = [r for r in rows if (r.get("category") or "").lower() == category.lower()]

        plugins = []
        for p in rows:
            kf = p.get("key_features") or []
            if isinstance(kf, str):
                kf = json.loads(kf)
            _raw_icon = p.get("icon_url") or ""
            if _raw_icon.startswith("data:"):
                _icon_mime = _raw_icon.split(":")[1].split(";")[0] if ":" in _raw_icon else ""
                _icon_ext = _MIME_TO_EXT.get(_icon_mime, "png")
                icon = f"/catalog/api/plugins/{p['slug']}/icon.{_icon_ext}"
            elif _raw_icon or p.get("icon_path"):
                icon = f"/catalog/api/plugins/{p['slug']}/icon"
            else:
                icon = None
            plugins.append({
                "slug": p["slug"], "name": p["name"], "intent": p.get("intent") or "",
                "device_type": p.get("device_type") or "",
                "category": p.get("category") or "",
                "publisher": p.get("publisher") or "DNUM",
                "maturity": p.get("maturity") or "release",
                "maturity_label": maturity_labels.get(p.get("maturity"), "Stable"),
                "icon_url": icon,
                "latest_version": p.get("latest_version"),
                "install_count": p.get("install_count") or 0,
                "key_features": kf,
            })
        return _catalog_templates.TemplateResponse("catalog_index.html", {
            "request": request, "plugins": plugins,
            "categories": all_categories, "current_category": category,
        })
    finally:
        if pool_ctx is not None:
            pool_ctx.__exit__(None, None, None)
        elif conn is not None:
            conn.close()


def _serve_plugin_download(slug: str, version_filter: str | None = None):
    """Resolve and serve a plugin binary. version_filter=None → latest published."""
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        raise HTTPException(404, "Aucune version disponible")
    conn = None
    pool_ctx = _pooled_conn()
    try:
        if pool_ctx is not None:
            conn = pool_ctx.__enter__()
        else:
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT id, device_type FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
            prow = cur.fetchone()
            if not prow:
                raise HTTPException(404, "Plugin introuvable")
            plugin_id, device_type = prow[0], prow[1]

            if version_filter:
                cur.execute("""
                    SELECT pv.version, pv.distribution_mode, pv.download_url, pv.artifact_id
                    FROM plugin_versions pv
                    WHERE pv.plugin_id = %s AND pv.version = %s AND pv.status = 'published'
                    LIMIT 1
                """, (plugin_id, version_filter))
            else:
                cur.execute("""
                    SELECT pv.version, pv.distribution_mode, pv.download_url, pv.artifact_id
                    FROM plugin_versions pv
                    WHERE pv.plugin_id = %s AND pv.status = 'published'
                    ORDER BY pv.published_at DESC NULLS LAST
                    LIMIT 1
                """, (plugin_id,))
            vrow = cur.fetchone()
            if not vrow:
                raise HTTPException(404, "Aucune version disponible")
            version, dist_mode, download_url, artifact_id = vrow

            ext = _DEVICE_TYPE_EXT.get(device_type, "bin")
            filename = f"{slug}-{version}.{ext}"

            if dist_mode == "managed" and artifact_id:
                cur.execute("SELECT s3_path FROM artifacts WHERE id = %s", (artifact_id,))
                arow = cur.fetchone()
                if arow and arow[0]:
                    s3_path = arow[0]
                    if not os.path.isfile(s3_path):
                        # Pull-on-miss: fetch from admin pod and cache locally
                        _pull_binary_from_admin(s3_path)
                    if os.path.isfile(s3_path):
                        return FileResponse(s3_path, filename=filename,
                                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
                    # Try S3 presigned URL
                    try:
                        client = s3_client()
                        if client:
                            bucket = os.getenv("S3_BUCKET", "device-management")
                            presigned = client.generate_presigned_url(
                                "get_object", Params={"Bucket": bucket, "Key": s3_path}, ExpiresIn=300
                            )
                            return RedirectResponse(presigned, status_code=302)
                    except Exception:
                        pass
                raise HTTPException(404, "Fichier binaire introuvable")

            if dist_mode in ("download_link", "store") and download_url:
                return RedirectResponse(download_url, status_code=302)

            raise HTTPException(404, "Aucune version disponible")
    finally:
        if pool_ctx is not None:
            pool_ctx.__exit__(None, None, None)
        elif conn is not None:
            conn.close()


@app.get("/catalog/{slug}/download/{filename}")
def catalog_download_file(slug: str, filename: str):
    """Public — download by filename (e.g. mirai-libreoffice-0.2.1.oxt)."""
    # Strip known extensions, then remove slug prefix to get version
    _known_ext = (".oxt", ".xpi", ".crx", ".bin")
    base = filename
    for ext in _known_ext:
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    version = base.removeprefix(f"{slug}-") if base.startswith(f"{slug}-") else base
    return _serve_plugin_download(slug, version_filter=version if version != filename else None)


@app.get("/catalog/{slug}/download")
def catalog_download(slug: str):
    """Public — redirect to latest version with proper filename."""
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        raise HTTPException(404, "Aucune version disponible")
    conn = None
    pool_ctx = _pooled_conn()
    try:
        if pool_ctx is not None:
            conn = pool_ctx.__enter__()
        else:
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT id, device_type FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
            prow = cur.fetchone()
            if not prow:
                raise HTTPException(404, "Plugin introuvable")
            plugin_id, device_type = prow[0], prow[1]
            cur.execute("""
                SELECT pv.version FROM plugin_versions pv
                WHERE pv.plugin_id = %s AND pv.status = 'published'
                ORDER BY pv.published_at DESC NULLS LAST LIMIT 1
            """, (plugin_id,))
            vrow = cur.fetchone()
            if not vrow:
                raise HTTPException(404, "Aucune version disponible")
            version = vrow[0]
            ext = _DEVICE_TYPE_EXT.get(device_type, "bin")
            return RedirectResponse(
                f"/catalog/{slug}/download/{slug}-{version}.{ext}",
                status_code=302,
            )
    finally:
        if pool_ctx is not None:
            pool_ctx.__exit__(None, None, None)
        elif conn is not None:
            conn.close()


@app.get("/catalog/{slug}", response_class=Response)
def catalog_detail(request: Request, slug: str):
    """Public HTML — plugin detail page."""
    db_url = _db_url_bootstrap() or _db_url()
    if not psycopg2 or not db_url:
        raise HTTPException(404)
    maturity_labels = {"dev": "Dev", "alpha": "Alpha", "beta": "Beta",
                       "pre-release": "Pre-release", "release": "Stable"}
    conn = None
    pool_ctx = _pooled_conn()
    try:
        if pool_ctx is not None:
            conn = pool_ctx.__enter__()
        else:
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM plugins WHERE slug = %s AND status = 'active'", (slug,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Plugin introuvable")
            cols = [d[0] for d in cur.description]
            p = dict(zip(cols, row))

            cur.execute("""
                SELECT version, release_notes FROM plugin_versions
                WHERE plugin_id = %s AND status = 'published'
                ORDER BY published_at DESC LIMIT 1
            """, (p["id"],))
            vrow = cur.fetchone()

            cur.execute(
                "SELECT COUNT(DISTINCT client_uuid) FROM plugin_installations WHERE plugin_id=%s AND status='active'",
                (p["id"],),
            )
            installs = cur.fetchone()[0]

        kf = p.get("key_features") or []
        if isinstance(kf, str):
            kf = json.loads(kf)

        _raw_icon = p.get("icon_url") or ""
        if _raw_icon.startswith("data:"):
            _icon_mime = _raw_icon.split(":")[1].split(";")[0] if ":" in _raw_icon else ""
            _icon_ext = _MIME_TO_EXT.get(_icon_mime, "png")
            icon = f"/catalog/api/plugins/{p['slug']}/icon.{_icon_ext}"
        elif _raw_icon or p.get("icon_path"):
            icon = f"/catalog/api/plugins/{p['slug']}/icon"
        else:
            icon = None

        device_type = p.get("device_type") or ""
        file_ext = _DEVICE_TYPE_EXT.get(device_type)

        updated_at = p.get("updated_at")
        if updated_at:
            try:
                updated_at = updated_at.strftime("%d %B %Y")
            except Exception:
                updated_at = str(updated_at)[:10]

        plugin = {
            "slug": p["slug"], "name": p["name"],
            "description": p.get("description") or "",
            "intent": p.get("intent") or "",
            "device_type": device_type,
            "category": p.get("category") or "",
            "publisher": p.get("publisher") or "DNUM",
            "maturity": p.get("maturity") or "release",
            "maturity_label": maturity_labels.get(p.get("maturity"), "Stable"),
            "icon_url": icon,
            "latest_version": vrow[0] if vrow else None,
            "changelog_summary": (vrow[1] or "") if vrow else "",
            "install_count": installs,
            "key_features": kf,
            "source_url": p.get("source_url"),
            "homepage_url": p.get("homepage_url"),
            "support_email": p.get("support_email"),
            "doc_url": p.get("doc_url"),
            "license": p.get("license"),
            "updated_at": updated_at,
        }
        return _catalog_templates.TemplateResponse("catalog_detail.html", {
            "request": request, "plugin": plugin, "file_ext": file_ext,
        })
    finally:
        if pool_ctx is not None:
            pool_ctx.__exit__(None, None, None)
        elif conn is not None:
            conn.close()


# ─── Files API (internal, token-secured, read-only) ──────────────────────
# Used by admin pods to list/inspect cached binaries on API pods (debug).
# Binaries are pulled on-demand from admin via _pull_binary_from_admin().

def _files_admin_guard(request: Request) -> None:
    """Verify X-Admin-Token header for the files API."""
    expected = (settings.queue_admin_token or "").strip()
    if not expected:
        raise HTTPException(403, "Files API token not configured")
    provided = (request.headers.get("x-admin-token") or "").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(403, "Invalid token")


@app.get("/api/files")
def files_list(request: Request, prefix: str = ""):
    """List cached binaries. Optional ?prefix=libreoffice/"""
    _files_admin_guard(request)
    base = settings.local_binaries_dir
    if not os.path.isdir(base):
        return JSONResponse({"files": []})
    target = _safe_path_join(base, prefix) if prefix else base
    if not os.path.isdir(target):
        return JSONResponse({"files": []})
    result = []
    for root, _dirs, files in os.walk(target):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base)
            result.append({"path": rel, "size": os.path.getsize(full)})
    result.sort(key=lambda x: x["path"])
    return JSONResponse({"files": result, "total": len(result)})


@app.get("/binaries/{path:path}")
def get_binary(path: str):
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
        logger.exception("Failed to log binary call")

    if settings.binaries_mode == "local":
        local_path = _safe_path_join(settings.local_binaries_dir, path)
        if not os.path.isfile(local_path):
            raise HTTPException(status_code=404, detail="Local binary not found.")
        return FileResponse(local_path, media_type="application/octet-stream")

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

            return StreamingResponse(iterfile(), media_type=content_type)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Binary not found: {e!r}")

    raise HTTPException(status_code=500, detail="Invalid DM_BINARIES_MODE (must be presign or proxy or local).")

# ---- Relay-assistant reverse proxy (dev / single-origin setups) ----
# In production a front reverse-proxy routes /relay-assistant/* to the nginx
# relay service.  In docker-compose dev the relay is a separate container
# (relay-assistant:8080) so we proxy here to keep the plugin's single-origin
# assumption working.

_RELAY_UPSTREAM = os.getenv(
    "DM_RELAY_ASSISTANT_UPSTREAM", "http://relay-assistant:8080"
).rstrip("/")

_RELAY_HOP_HEADERS = frozenset({
    "host", "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
})


@app.api_route("/relay-assistant/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def relay_assistant_proxy(path: str, request: Request):
    upstream_url = f"{_RELAY_UPSTREAM}/{path}"
    qs = str(request.url.query)
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _RELAY_HOP_HEADERS
    }
    body = await request.body()

    async with httpx.AsyncClient(timeout=30) as client:
        upstream_resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        )

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in _RELAY_HOP_HEADERS
    }
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )


# ---- Local entrypoint (VS Code friendly)
# Allows debugging by running this file directly (e.g. VS Code: "Python: Current File").
# In production, prefer: uvicorn app.main:app --host 0.0.0.0 --port 3001

def _get_port() -> int:
    try:
        return int(os.getenv("DM_PORT", os.getenv("PORT", "8000")))
    except ValueError:
        return 8000


if __name__ == "__main__":
    if str(settings.runtime_mode or "api").strip().lower() == "worker":
        _run_queue_worker_loop(stop_event=None, once=False)
    else:
        reload_enabled = os.getenv("RELOAD", "false").lower() in ("1", "true", "yes")
        workers = max(1, int(settings.uvicorn_workers or 1))
        if reload_enabled and workers > 1:
            logger.warning("RELOAD=true is incompatible with DM_UVICORN_WORKERS>1; forcing workers=1")
            workers = 1
        uvicorn.run(
            "app.main:app",
            host=os.getenv("HOST", "0.0.0.0"),
            port=_get_port(),
            reload=reload_enabled,
            workers=workers,
            log_level=os.getenv("LOG_LEVEL", "info"),
            access_log=bool(settings.uvicorn_access_log),
            timeout_keep_alive=max(1, int(settings.uvicorn_timeout_keep_alive)),
        )


@app.on_event("startup")
def _startup_db_init() -> None:
    # Fire-and-forget startup check: useful diagnostics without blocking pod startup.
    _start_s3_connectivity_check_non_blocking()

    if psycopg2 is None:
        logger.warning("psycopg2 not installed; skipping DB bootstrap/schema init")
        return
    base_url = _db_url()
    if not base_url:
        return
    admin_url: str | None = None
    try:
        admin_url = _admin_db_url(base_url)
        if admin_url:
            admin_url = _with_db(admin_url, "postgres")
            _wait_for_db(admin_url, timeout_seconds=30, interval_seconds=1.0)
            try:
                _ensure_dev_role(admin_url)
            except psycopg2.Error:
                logger.warning("Skipping dev role creation/alter (insufficient privilege)")
            _ensure_database_exists(admin_url, "bootstrap")
        else:
            logger.warning("No admin database URL available; schema will use app DB credentials only.")
    except Exception:
        logger.exception("Failed to ensure database bootstrap exists")
    try:
        bootstrap_url = _db_url_bootstrap()
        admin_bootstrap_url = _with_db(admin_url, "bootstrap") if admin_url else None
        if admin_bootstrap_url:
            _wait_for_db(admin_bootstrap_url, timeout_seconds=30, interval_seconds=1.0)
            _apply_schema(admin_bootstrap_url)
            _ensure_dev_privileges(admin_bootstrap_url)
        elif bootstrap_url:
            _wait_for_db(bootstrap_url, timeout_seconds=30, interval_seconds=1.0)
            _apply_schema(bootstrap_url)
        else:
            logger.warning("No bootstrap database URL available; skipping schema apply.")
    except Exception:
        logger.exception("Failed to apply DB schema")


@app.on_event("startup")
def _startup_embedded_queue_worker() -> None:
    global _embedded_worker_thread, _embedded_worker_stop
    mode = str(settings.runtime_mode or "api").strip().lower()
    if mode != "all":
        return
    if not settings.queue_enabled:
        return
    if _embedded_worker_thread and _embedded_worker_thread.is_alive():
        return
    _embedded_worker_stop = threading.Event()
    _embedded_worker_thread = threading.Thread(
        target=_run_queue_worker_loop,
        kwargs={"stop_event": _embedded_worker_stop, "once": False},
        daemon=True,
        name="dm-embedded-queue-worker",
    )
    _embedded_worker_thread.start()
    logger.info("Embedded queue worker started (mode=all).")


@app.on_event("shutdown")
def _shutdown_embedded_queue_worker() -> None:
    global _embedded_worker_thread, _embedded_worker_stop
    if _embedded_worker_stop:
        _embedded_worker_stop.set()
    if _embedded_worker_thread and _embedded_worker_thread.is_alive():
        _embedded_worker_thread.join(timeout=5)
    _embedded_worker_thread = None
    _embedded_worker_stop = None
