"""Runtime configuration overrides — admin-editable, persisted in PostgreSQL,
propagated across all pods.

Design (see plan): the ENV injected at deploy time is the *baseline*, snapshotted
once at startup. Admin overrides live in the `config_overrides` table and take
precedence. On reload, each pod re-applies the effective state by mutating
``os.environ`` (covers the many ``os.getenv()`` call sites) and, for Pydantic
``Settings`` fields read per-request, ``setattr(settings, field, value)`` — so no
existing call site needs rewriting for hot-reloadable keys.

Propagation: a single-row generation counter (``config_state``) is polled every
few seconds; a Postgres ``LISTEN/NOTIFY`` wakes the loop instantly. The poll is
the source of truth (a missed NOTIFY is caught next tick). Each pod enrolls into
``config_pod_state`` and heartbeats its applied generation + health metrics.

Readiness: ``_config_ready`` stays False until the first successful DB reload, so
a cold-started pod that cannot read its config does not serve (Stage 9).
"""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

from .services import health
from .services.crypto import mask_secret

logger = logging.getLogger("device-management.runtime_config")

try:
    import psycopg2
except ModuleNotFoundError:  # pragma: no cover
    psycopg2 = None  # type: ignore

NOTIFY_CHANNEL = "dm_config_reload"


# ── Editable-key registry (single source of truth for what may be overridden) ──
@dataclass(frozen=True)
class ConfigKeySpec:
    env_name: str                 # canonical ENV var name (and override table key)
    label: str                    # human label (FR) for the UI
    group: str                    # UI grouping
    type: str                     # bool | int | float | str | list
    hot_reloadable: bool          # True = effective without restart
    sensitive: bool = False       # secret: encrypted at rest, masked in UI
    settings_field: str | None = None  # Pydantic Settings field to setattr, if any
    min: float | None = None
    max: float | None = None


def _spec(*args, **kwargs) -> ConfigKeySpec:
    return ConfigKeySpec(*args, **kwargs)


# NOTE: keep this list aligned with the plan's registre table. Only keys here are
# editable; anything else is rejected by the admin API.
EDITABLE_KEYS: dict[str, ConfigKeySpec] = {
    # ── Plugins-common ──
    "API_BASE": _spec("API_BASE", "API base (plugins)", "Plugins", "str", True),
    "RELAY_ASSISTANT_BASE_URL": _spec(
        "RELAY_ASSISTANT_BASE_URL", "Relay assistant base URL", "Plugins", "str", False),
    "COMPTE_RENDU_URL": _spec("COMPTE_RENDU_URL", "URL compte-rendu", "Plugins", "str", True),
    "COMU_URL": _spec("COMU_URL", "URL communications", "Plugins", "str", True),
    "PUBLIC_BASE_URL": _spec(
        "PUBLIC_BASE_URL", "URL publique (plugins ; login admin = redémarrage)", "Plugins", "str", True),
    "DM_BOOTSTRAP_URLS": _spec(
        "DM_BOOTSTRAP_URLS", "URLs de bootstrap (ordre = priorité)", "Plugins", "list", True),
    # ── Keycloak ──
    "KEYCLOAK_ISSUER_URL": _spec("KEYCLOAK_ISSUER_URL", "Keycloak issuer URL", "Keycloak", "str", False),
    "KEYCLOAK_REALM": _spec("KEYCLOAK_REALM", "Keycloak realm", "Keycloak", "str", False),
    "KEYCLOAK_CLIENT_ID": _spec("KEYCLOAK_CLIENT_ID", "Keycloak client ID", "Keycloak", "str", False),
    "KEYCLOAK_REDIRECT_URI": _spec("KEYCLOAK_REDIRECT_URI", "Keycloak redirect URI", "Keycloak", "str", False),
    "KEYCLOAK_ALLOWED_REDIRECT_URI": _spec(
        "KEYCLOAK_ALLOWED_REDIRECT_URI", "Keycloak allowed redirect URI", "Keycloak", "str", True),
    # ── LLM ──
    "LLM_BASE_URL": _spec("LLM_BASE_URL", "LLM base URL", "LLM", "str", True),
    "LLM_API_TOKEN": _spec("LLM_API_TOKEN", "LLM API token", "LLM", "str", True, sensitive=True),
    "DEFAULT_MODEL_NAME": _spec("DEFAULT_MODEL_NAME", "Modèle par défaut", "LLM", "str", True),
    # ── Télémétrie ──
    "DM_TELEMETRY_ENABLED": _spec(
        "DM_TELEMETRY_ENABLED", "Télémétrie active", "Télémétrie", "bool", True,
        settings_field="telemetry_enabled"),
    "DM_TELEMETRY_PUBLIC_ENDPOINT": _spec(
        "DM_TELEMETRY_PUBLIC_ENDPOINT", "Endpoint public télémétrie", "Télémétrie", "str", True,
        settings_field="telemetry_public_endpoint"),
    "DM_TELEMETRY_UPSTREAM_ENDPOINT": _spec(
        "DM_TELEMETRY_UPSTREAM_ENDPOINT", "Endpoint upstream télémétrie", "Télémétrie", "str", True,
        settings_field="telemetry_upstream_endpoint"),
    "DM_TELEMETRY_UPSTREAM_KEY": _spec(
        "DM_TELEMETRY_UPSTREAM_KEY", "Clé upstream télémétrie", "Télémétrie", "str", True,
        sensitive=True, settings_field="telemetry_upstream_key"),
    "DM_TELEMETRY_TOKEN_SIGNING_KEY": _spec(
        "DM_TELEMETRY_TOKEN_SIGNING_KEY", "Clé de signature des tokens télémétrie", "Télémétrie", "str", True,
        sensitive=True, settings_field="telemetry_token_signing_key"),
    "DM_TELEMETRY_REQUIRE_TOKEN": _spec(
        "DM_TELEMETRY_REQUIRE_TOKEN", "Token télémétrie requis", "Télémétrie", "bool", True,
        settings_field="telemetry_require_token"),
}


# ── Module state ──────────────────────────────────────────────────────────
_LOCK = threading.RLock()
_BASELINE_PY: dict[str, Any] = {}        # env baseline, captured once at startup
_OVERRIDES_META: dict[str, dict] = {}    # key -> {value, is_secret, updated_by, updated_at}
_applied_generation: int = -1
_config_ready: bool = False
_baseline_ready: bool = False


# ── Type coercion ─────────────────────────────────────────────────────────
def _parse_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    s = str(raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return [str(x) for x in json.loads(s)]
        except Exception:
            pass
    return [item.strip() for item in s.split(",") if item.strip()]


def _coerce(value: Any, vtype: str) -> Any:
    if value is None:
        return None
    if vtype == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if vtype == "int":
        return int(value)
    if vtype == "float":
        return float(value)
    if vtype == "list":
        return _parse_list(value)
    return str(value)


def _to_env_str(value: Any, vtype: str) -> str:
    if value is None:
        return ""
    if vtype == "bool":
        return "true" if _coerce(value, "bool") else "false"
    if vtype == "list":
        return json.dumps(_parse_list(value))
    return str(value)


def coerce_input(spec: ConfigKeySpec, value: Any) -> Any:
    """Coerce + range-check an admin-supplied value. Raises ValueError on bad input."""
    py = _coerce(value, spec.type)
    if spec.type in ("int", "float") and py is not None:
        if spec.min is not None and py < spec.min:
            raise ValueError(f"{spec.env_name} < {spec.min}")
        if spec.max is not None and py > spec.max:
            raise ValueError(f"{spec.env_name} > {spec.max}")
    if spec.type == "list":
        cleaned, seen = [], set()
        for item in py:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                cleaned.append(item)
        return cleaned
    return py


# ── Baseline snapshot (once at startup) ───────────────────────────────────
def snapshot_baseline(force: bool = False) -> None:
    """Capture the ENV baseline for every editable key. Idempotent."""
    global _baseline_ready
    with _LOCK:
        if _baseline_ready and not force:
            return
        from .settings import settings
        for env, spec in EDITABLE_KEYS.items():
            if spec.settings_field:
                _BASELINE_PY[env] = getattr(settings, spec.settings_field, None)
            else:
                raw = os.getenv(env, "")
                _BASELINE_PY[env] = _coerce(raw, spec.type) if raw != "" else (
                    [] if spec.type == "list" else "" if spec.type == "str" else None)
        _baseline_ready = True


# ── Apply effective state to the live process ─────────────────────────────
def _apply_one(spec: ConfigKeySpec, py: Any) -> None:
    os.environ[spec.env_name] = _to_env_str(py, spec.type)
    if spec.settings_field is not None:
        try:
            from .settings import settings
            setattr(settings, spec.settings_field, py)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("setattr settings.%s failed: %s", spec.settings_field, exc)


def apply_state(overrides_py: dict[str, Any]) -> None:
    """Apply effective = {**baseline, **overrides} to os.environ + settings.

    Idempotent: keys without an override are restored to baseline.
    """
    with _LOCK:
        for env, spec in EDITABLE_KEYS.items():
            py = overrides_py[env] if env in overrides_py else _BASELINE_PY.get(env)
            _apply_one(spec, py)


# ── Override-aware accessor (for new code / future dynamic values) ────────
def cfg(env_name: str, default: Any = None, as_list: bool = False) -> Any:
    """Return the effective value (override applied via os.environ) for a key."""
    val = os.environ.get(env_name)
    if val is None:
        val = default
    if as_list:
        return _parse_list(val if val is not None else "")
    return val


def env_baseline(env_name: str) -> Any:
    with _LOCK:
        return _BASELINE_PY.get(env_name)


# ── Effective view (drives the admin API + UI) ────────────────────────────
def _display(py: Any, spec: ConfigKeySpec) -> Any:
    if py is None:
        return "" if spec.type != "list" else []
    if spec.sensitive:
        return mask_secret(str(py))
    if spec.type == "list":
        return _parse_list(py)
    return py


def effective_view() -> list[dict]:
    """Per-key baseline vs effective + diff metadata for the UI/API."""
    out = []
    with _LOCK:
        for env, spec in EDITABLE_KEYS.items():
            baseline_py = _BASELINE_PY.get(env)
            meta = _OVERRIDES_META.get(env)
            override_present = meta is not None
            effective_py = meta["value"] if override_present else baseline_py
            modified = override_present and (effective_py != baseline_py)
            out.append({
                "key": env,
                "label": spec.label,
                "group": spec.group,
                "type": spec.type,
                "hot_reloadable": spec.hot_reloadable,
                "sensitive": spec.sensitive,
                "baseline": _display(baseline_py, spec),
                "effective": _display(effective_py, spec),
                "override_present": override_present,
                "modified": modified,
                "updated_by": meta.get("updated_by") if meta else None,
                "updated_at": meta["updated_at"].isoformat()
                if meta and meta.get("updated_at") else None,
            })
    return out


# ── Readiness & request gate (Stage 9) ───────────────────────────────────
# The gate is only *active* once the background sync has been started on a real
# pod (DB present). In tests / DB-less runs it stays inactive so requests are
# never blocked.
_gate_enabled: bool = False


def enable_request_gate() -> None:
    global _gate_enabled
    _gate_enabled = True


def disable_request_gate() -> None:
    global _gate_enabled
    _gate_enabled = False


def is_config_ready() -> bool:
    return _config_ready


def should_gate_requests() -> bool:
    """True if this pod must refuse business traffic (config never loaded)."""
    return _gate_enabled and not _config_ready


def applied_generation() -> int:
    return _applied_generation


def wait_until_ready(timeout: float) -> bool:
    """Block up to `timeout` seconds for the first successful config load."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _config_ready:
            return True
        time.sleep(0.2)
    return _config_ready


def start_background(stop_event: threading.Event, role: str | None = None) -> list[threading.Thread]:
    """Start the sync loop + NOTIFY listener as daemon threads. Reusable by the
    FastAPI lifespan and the worker entrypoint."""
    snapshot_baseline()
    threads = [
        threading.Thread(target=run_config_sync_loop, args=(stop_event, role),
                         daemon=True, name="dm-config-sync"),
        threading.Thread(target=run_notify_listener, args=(stop_event,),
                         daemon=True, name="dm-config-notify"),
    ]
    for t in threads:
        t.start()
    return threads


# ── DB helpers ────────────────────────────────────────────────────────────
def _bootstrap_url() -> str | None:
    from .services.db import db_url_bootstrap
    return db_url_bootstrap()


def get_current_generation(conn) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT generation FROM config_state WHERE id = TRUE")
        row = cur.fetchone()
    return int(row[0]) if row else None


def reload_overrides(conn) -> int | None:
    """Reload all overrides from DB, apply them, mark ready. Returns generation."""
    global _applied_generation, _config_ready, _OVERRIDES_META
    from .services.crypto import decrypt_secret
    gen = get_current_generation(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value, value_type, is_secret, updated_by, updated_at "
            "FROM config_overrides")
        rows = cur.fetchall()
    overrides_py: dict[str, Any] = {}
    meta: dict[str, dict] = {}
    for key, value, vtype, is_secret, updated_by, updated_at in rows:
        if key not in EDITABLE_KEYS:
            continue  # ignore stale/unknown keys
        try:
            raw = decrypt_secret(value) if is_secret else value
            py = _coerce(raw, vtype)
        except Exception as exc:
            logger.warning("skipping override %s (decode failed): %s", key, exc)
            continue
        overrides_py[key] = py
        meta[key] = {"value": py, "is_secret": is_secret,
                     "updated_by": updated_by, "updated_at": updated_at}
    with _LOCK:
        _OVERRIDES_META = meta
    apply_state(overrides_py)
    _applied_generation = gen if gen is not None else _applied_generation
    _config_ready = True
    return gen


def bump_generation(conn, actor: str | None) -> int:
    """Increment the generation and NOTIFY listeners (payload = generation only)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE config_state SET generation = generation + 1, updated_at = now(), "
            "updated_by = %s WHERE id = TRUE RETURNING generation", (actor,))
        gen = int(cur.fetchone()[0])
        cur.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, str(gen)))
    return gen


def force_local_reload() -> int | None:
    """Open a short-lived connection, reload overrides immediately. Best-effort."""
    from .services.db import get_db_connection
    conn = get_db_connection()
    if conn is None:
        return None
    try:
        conn.autocommit = True
        return reload_overrides(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Pod enrollment + heartbeat ────────────────────────────────────────────
def _pod_identity(role: str | None) -> dict:
    return {
        "pod_name": socket.gethostname(),
        "node_name": os.getenv("NODE_NAME"),
        "runtime_mode": role or os.getenv("DM_RUNTIME_MODE", "api"),
        "pid": os.getpid(),
        "pod_ip": os.getenv("POD_IP"),
        "port": int(os.getenv("DM_PORT", "3001") or 3001),
        "app_version": os.getenv("DM_APP_VERSION") or os.getenv("APP_VERSION") or "dev",
    }


def enroll(conn, role: str | None) -> None:
    """Register this pod (process start). Increments restart_count on re-enroll."""
    ident = _pod_identity(role)
    h = health.read_health()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO config_pod_state
                (pod_name, node_name, runtime_mode, pid, pod_ip, port,
                 applied_generation, app_version, restart_count,
                 rss_bytes, mem_limit_bytes, load1, cpu_count, requests_total,
                 started_at, last_heartbeat_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s, now(), now())
            ON CONFLICT (pod_name) DO UPDATE SET
                restart_count = config_pod_state.restart_count + 1,
                node_name = EXCLUDED.node_name,
                runtime_mode = EXCLUDED.runtime_mode,
                pid = EXCLUDED.pid,
                pod_ip = EXCLUDED.pod_ip,
                port = EXCLUDED.port,
                applied_generation = EXCLUDED.applied_generation,
                app_version = EXCLUDED.app_version,
                rss_bytes = EXCLUDED.rss_bytes,
                mem_limit_bytes = EXCLUDED.mem_limit_bytes,
                load1 = EXCLUDED.load1,
                cpu_count = EXCLUDED.cpu_count,
                requests_total = EXCLUDED.requests_total,
                started_at = now(),
                last_heartbeat_at = now()
            """,
            (ident["pod_name"], ident["node_name"], ident["runtime_mode"], ident["pid"],
             ident["pod_ip"], ident["port"], _applied_generation, ident["app_version"],
             h["rss_bytes"], h["mem_limit_bytes"], h["load1"], h["cpu_count"], h["requests_total"]),
        )


def heartbeat(conn, role: str | None) -> None:
    """Refresh heartbeat + health + applied generation (does not touch restart_count)."""
    ident = _pod_identity(role)
    h = health.read_health()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE config_pod_state SET
                applied_generation = %s,
                runtime_mode = %s,
                pod_ip = %s,
                rss_bytes = %s,
                mem_limit_bytes = %s,
                load1 = %s,
                cpu_count = %s,
                requests_total = %s,
                last_heartbeat_at = now()
            WHERE pod_name = %s
            """,
            (_applied_generation, ident["runtime_mode"], ident["pod_ip"],
             h["rss_bytes"], h["mem_limit_bytes"], h["load1"], h["cpu_count"],
             h["requests_total"], ident["pod_name"]),
        )


# ── Background loops ──────────────────────────────────────────────────────
_wake = threading.Event()
_loop_conn = None
_loop_conn_lock = threading.Lock()


def request_wake() -> None:
    """Wake the sync loop immediately (called by the NOTIFY listener / shutdown)."""
    _wake.set()


def _get_loop_conn():
    global _loop_conn
    with _loop_conn_lock:
        if _loop_conn is not None:
            return _loop_conn
        from .services.db import get_db_connection
        conn = get_db_connection()
        if conn is not None:
            conn.autocommit = True
            _loop_conn = conn
        return _loop_conn


def _reset_loop_conn() -> None:
    global _loop_conn
    with _loop_conn_lock:
        if _loop_conn is not None:
            try:
                _loop_conn.close()
            except Exception:
                pass
        _loop_conn = None


def _poll_interval() -> float:
    try:
        return max(0.5, float(os.getenv("DM_CONFIG_POLL_INTERVAL_SECONDS", "3")))
    except ValueError:
        return 3.0


def run_config_sync_loop(stop_event: threading.Event, role: str | None = None) -> None:
    """Per-pod loop: poll generation, reload on change, enroll/heartbeat. Resilient."""
    snapshot_baseline()
    enrolled = False
    logger.info("config sync loop started (role=%s)", role)
    while not stop_event.is_set():
        try:
            conn = _get_loop_conn()
            if conn is None:
                logger.debug("config sync: DB unavailable, retrying")
            else:
                gen = get_current_generation(conn)
                if gen is not None and gen != _applied_generation:
                    reload_overrides(conn)
                if not enrolled:
                    enroll(conn, role)
                    enrolled = True
                else:
                    heartbeat(conn, role)
        except Exception as exc:
            logger.warning("config sync loop iteration failed: %s", exc)
            _reset_loop_conn()
            enrolled = False
        _wake.wait(_poll_interval() + random.uniform(0, 0.5))  # noqa: S311 - jitter, not crypto
        _wake.clear()
    _reset_loop_conn()
    logger.info("config sync loop stopped (role=%s)", role)


def run_notify_listener(stop_event: threading.Event) -> None:
    """Dedicated LISTEN connection; on NOTIFY, wake the sync loop. Optional accelerator."""
    if psycopg2 is None:
        return
    import select
    logger.info("config NOTIFY listener started")
    while not stop_event.is_set():
        conn = None
        try:
            url = _bootstrap_url()
            if not url:
                stop_event.wait(5)
                continue
            conn = psycopg2.connect(url)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {NOTIFY_CHANNEL}")
            while not stop_event.is_set():
                if select.select([conn], [], [], 5)[0]:
                    conn.poll()
                    had = bool(conn.notifies)
                    conn.notifies.clear()
                    if had:
                        request_wake()  # poll loop will see the new generation
        except Exception as exc:
            logger.warning("config NOTIFY listener error: %s", exc)
            stop_event.wait(2)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    logger.info("config NOTIFY listener stopped")
