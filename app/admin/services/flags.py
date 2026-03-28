"""Feature flags service — CRUD operations."""

from __future__ import annotations


def list_flags(cur) -> list[dict]:
    cur.execute("""
        SELECT ff.id, ff.name, ff.description, ff.default_value,
               ff.created_at, ff.updated_at,
               COUNT(ffo.cohort_id) AS override_count
        FROM feature_flags ff
        LEFT JOIN feature_flag_overrides ffo ON ffo.feature_id = ff.id
        GROUP BY ff.id
        ORDER BY ff.name
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_flag(cur, flag_id: int) -> dict | None:
    cur.execute("""
        SELECT id, name, description, default_value, created_at, updated_at
        FROM feature_flags WHERE id = %s
    """, (flag_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_flag_overrides(cur, flag_id: int) -> list[dict]:
    cur.execute("""
        SELECT ffo.feature_id, ffo.cohort_id, ffo.value,
               ffo.min_plugin_version, ffo.updated_at,
               c.name AS cohort_name
        FROM feature_flag_overrides ffo
        JOIN cohorts c ON c.id = ffo.cohort_id
        WHERE ffo.feature_id = %s
        ORDER BY c.name
    """, (flag_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_flag(cur, *, name: str, description: str, default_value: bool) -> int:
    cur.execute("""
        INSERT INTO feature_flags (name, description, default_value)
        VALUES (%s, %s, %s)
        RETURNING id
    """, (name, description, default_value))
    return cur.fetchone()[0]


def update_flag_default(cur, flag_id: int, value: bool) -> bool:
    cur.execute("""
        UPDATE feature_flags SET default_value = %s, updated_at = NOW()
        WHERE id = %s RETURNING id
    """, (value, flag_id))
    return cur.fetchone() is not None


def create_override(cur, *, feature_id: int, cohort_id: int, value: bool,
                    min_plugin_version: str = None) -> None:
    cur.execute("""
        INSERT INTO feature_flag_overrides (feature_id, cohort_id, value, min_plugin_version)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (feature_id, cohort_id)
        DO UPDATE SET value = EXCLUDED.value,
                      min_plugin_version = EXCLUDED.min_plugin_version,
                      updated_at = NOW()
    """, (feature_id, cohort_id, value, min_plugin_version))


def delete_override(cur, feature_id: int, cohort_id: int) -> bool:
    cur.execute("""
        DELETE FROM feature_flag_overrides
        WHERE feature_id = %s AND cohort_id = %s
        RETURNING feature_id
    """, (feature_id, cohort_id))
    return cur.fetchone() is not None
