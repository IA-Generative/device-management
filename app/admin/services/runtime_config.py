"""Runtime config overrides service — SQL CRUD + fleet/propagation queries.

Routes delegate here; orchestration (coercion, encryption, generation bump,
audit) lives in the router. This module only touches the DB.
"""
from __future__ import annotations


def get_state(cur) -> dict:
    cur.execute("SELECT generation, updated_at, updated_by FROM config_state WHERE id = TRUE")
    row = cur.fetchone()
    if not row:
        return {"generation": 0, "updated_at": None, "updated_by": None}
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


def list_overrides(cur) -> list[dict]:
    cur.execute(
        "SELECT key, value, value_type, is_secret, updated_by, updated_at "
        "FROM config_overrides ORDER BY key")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_override(cur, key: str) -> dict | None:
    cur.execute(
        "SELECT key, value, value_type, is_secret, updated_by, updated_at "
        "FROM config_overrides WHERE key = %s", (key,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


def set_override(cur, *, key: str, value: str, value_type: str,
                 is_secret: bool, actor_email: str | None) -> None:
    cur.execute(
        """
        INSERT INTO config_overrides (key, value, value_type, is_secret, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            value_type = EXCLUDED.value_type,
            is_secret = EXCLUDED.is_secret,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        """,
        (key, value, value_type, is_secret, actor_email),
    )


def delete_override(cur, key: str) -> bool:
    cur.execute("DELETE FROM config_overrides WHERE key = %s RETURNING key", (key,))
    return cur.fetchone() is not None


def list_pods(cur) -> list[dict]:
    cur.execute(
        """
        SELECT pod_name, node_name, runtime_mode, pod_ip, port,
               applied_generation, app_version, restart_count,
               rss_bytes, mem_limit_bytes, load1, cpu_count, requests_total,
               EXTRACT(EPOCH FROM (now() - last_heartbeat_at))::int AS heartbeat_age_s,
               EXTRACT(EPOCH FROM (now() - started_at))::int AS uptime_s
        FROM config_pod_state
        ORDER BY runtime_mode, pod_name
        """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def reap_stale_pods(cur, older_than_seconds: int = 3600) -> int:
    """Remove pods whose heartbeat is older than the threshold (GC). Returns count."""
    cur.execute(
        "DELETE FROM config_pod_state "
        "WHERE last_heartbeat_at < now() - (%s || ' seconds')::interval",
        (str(older_than_seconds),))
    return cur.rowcount
