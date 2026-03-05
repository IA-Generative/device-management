from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
import base64
import hmac
import hashlib
from urllib.parse import urlparse, urlunparse
from urllib import request as urllib_request
from urllib import error as urllib_error

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

import uvicorn
import boto3
from botocore.client import Config

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
TELEMETRY_MAX_BODY_BYTES = settings.telemetry_max_body_size_mb * 1024 * 1024
S3_BINARIES_PREFIX = settings.s3_prefix_binaries
_telemetry_signing_warning_emitted = False


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
            try:
                cur.execute(
                    """
                    INSERT INTO provisioning (email, device_name, client_uuid, status, encryption_key, comments)
                    VALUES (%s, %s, %s, 'ENROLLED', %s, %s)
                    """,
                    (email, device_name, client_uuid, encryption_key, "enroll"),
                )
            except psycopg2.Error as exc:
                if getattr(exc, "pgcode", None) != "23505":
                    raise
                cur.execute(
                    """
                    UPDATE provisioning
                    SET email = %s,
                        device_name = %s,
                        status = 'ENROLLED',
                        encryption_key = %s,
                        updated_at = now()
                    WHERE client_uuid = %s
                      AND status IN ('PENDING', 'ENROLLED')
                    """,
                    (email, device_name, encryption_key, client_uuid),
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


DEVICE_ALLOWLIST = {"matisse", "libreoffice", "chrome", "edge", "firefox", "misc"}


def _load_config_template(profile: str, device: str | None = None) -> dict:
    """Load a config template JSON from `config/`.

    Resolution order (device-specific first when provided):
    - config/<device>/config.<profile>.json
    - config/<device>/config.json
    - config/config.<profile>.json
    - config/config.json
    """
    bases: list[str] = []
    if settings.config_dir:
        bases.append(settings.config_dir)
    bases.append(os.path.join(_repo_root(), "config"))

    for base in bases:
        candidates = []
        if device:
            candidates.extend(
                [
                    os.path.join(base, device, f"config.{profile}.json"),
                    os.path.join(base, device, "config.json"),
                ]
            )
        candidates.extend(
            [
                os.path.join(base, f"config.{profile}.json"),
                os.path.join(base, "config.json"),
            ]
        )
        for p in candidates:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
    raise FileNotFoundError("No config template found in ./config (expected config.json)")


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


_SECRET_CONFIG_KEYS = {
    "llm_api_tokens",
    "tokenOWUI",
    "telemetryKey",
    "keycloak_client_secret",
    "keycloakClientSecret",
}

_RELAY_MEMORY_STORE: dict[str, dict] = {}


def _normalize_client_uuid(raw_value: str | None) -> str:
    raw = str(raw_value or "").strip()
    if not raw:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(raw))
    except Exception:
        # Keep deterministic fallback for non-UUID plugin ids.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _extract_access_token_from_request(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth[7:].strip()


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


def _email_from_access_token(token: str) -> str:
    payload = _parse_unverified_jwt_payload(token)
    email = payload.get("email") or payload.get("preferred_username") or payload.get("sub")
    if isinstance(email, str):
        return email.strip()
    return ""


def _relay_allowed_targets() -> list[str]:
    raw = str(settings.relay_allowed_targets_csv or "").strip()
    targets = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not targets:
        targets = ["keycloak", "config"]
    if "config" not in targets:
        targets.append("config")
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
    if target_norm and allowed_targets and target_norm not in allowed_targets:
        return False, f"target '{target_norm}' not allowed"

    return True, {
        "client_uuid": row.get("client_uuid", ""),
        "email": row.get("email", ""),
        "allowed_targets": allowed_targets,
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


def _scrub_secret_values(cfg: dict) -> dict:
    cfg_obj = dict(cfg)
    config_obj = cfg_obj.get("config")
    if not isinstance(config_obj, dict):
        return cfg_obj

    for secret_key in _SECRET_CONFIG_KEYS:
        if secret_key in config_obj:
            config_obj[secret_key] = ""

    # legacy aliases
    if "authHeaderKey" in config_obj:
        config_obj["authHeaderKey"] = ""

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
        relay_keycloak_base = f"{public_base}/relay-assistant/keycloak"
        config_obj["keycloakIssuerUrl"] = relay_keycloak_base
        config_obj["keycloakAuthorizationEndpoint"] = f"{relay_keycloak_base}/protocol/openid-connect/auth"
        config_obj["keycloakTokenEndpoint"] = f"{relay_keycloak_base}/protocol/openid-connect/token"
        config_obj["keycloakUserinfoEndpoint"] = f"{relay_keycloak_base}/protocol/openid-connect/userinfo"
        config_obj["relayAssistantBaseUrl"] = f"{public_base}/relay-assistant"

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



@app.get("/config/config.json")
def get_config(request: Request, profile: str | None = None, device: str | None = None):
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
    dev = (device or "").strip().lower()
    if dev and dev not in DEVICE_ALLOWLIST:
        return JSONResponse(status_code=400, content={"ok": False, "error": "device is not supported"})

    try:
        cfg = _load_config_template(prof, dev or None)
    except FileNotFoundError as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    # 1) generic substitution ${VARNAME}
    cfg = _substitute_env(cfg)

    # 2) targeted overrides (e.g. telemetrySel)
    cfg = _apply_overrides(cfg, profile=prof, device=dev or None)

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

    return JSONResponse(content=cfg, headers={"Cache-Control": "no-store"})


@app.get("/config/{device}/config.json")
def get_device_config(request: Request, device: str, profile: str | None = None):
    return get_config(request=request, profile=profile, device=device)


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
        if not token:
            raise HTTPException(status_code=401, detail="Missing telemetry Bearer token.")
        payload = _verify_telemetry_token(token)
        client_uuid = str(payload.get("jti") or "telemetry")
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

    return _forward_telemetry_to_upstream(
        body,
        content_type=request.headers.get("content-type", "application/json"),
        user_agent=request.headers.get("user-agent"),
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
    auth_email = _email_from_access_token(access_token)
    if not auth_email:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "Missing or invalid PKCE access token."},
        )

    device_name = str(body_obj.get("device_name", "")).strip()
    plugin_uuid = str(body_obj.get("plugin_uuid", "")).strip()
    email = auth_email

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

    relay_data = {}

    try:
        _email, client_uuid, fingerprint = _extract_identity(request, body_obj=body_obj)
        if plugin_uuid:
            client_uuid = plugin_uuid
        client_uuid = _normalize_client_uuid(client_uuid)
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

        relay_data = _mint_or_rotate_relay_credentials(client_uuid=client_uuid, email=email)

        _log_device_connection(
            action="ENROLL",
            email=email,
            client_uuid=client_uuid,
            encryption_key_fingerprint=fingerprint,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception("Failed to process enroll call")

    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "stored": stored,
            "relay": relay_data,
            "relayClientId": relay_data.get("client_id", ""),
            "relayClientKey": relay_data.get("client_key", ""),
            "relayKeyExpiresAt": relay_data.get("expires_at", 0),
        },
    )


@app.get("/binaries/{path:path}")
def get_binary(path: str):
    if settings.binaries_mode == "local":
        local_path = _safe_path_join(settings.local_binaries_dir, path)
        if not os.path.isfile(local_path):
            raise HTTPException(status_code=404, detail="Local binary not found.")
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
            logger.exception("Failed to log binary call (local)")
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

    raise HTTPException(status_code=500, detail="Invalid DM_BINARIES_MODE (must be presign or proxy or local).")

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
