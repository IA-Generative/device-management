"""Cohorts service — CRUD operations."""

from __future__ import annotations

import json


def list_cohorts(cur) -> list[dict]:
    cur.execute("""
        SELECT c.id, c.name, c.description, c.type, c.config,
               c.created_at, c.updated_at,
               COUNT(cm.identifier_value) AS member_count
        FROM cohorts c
        LEFT JOIN cohort_members cm ON cm.cohort_id = c.id
        GROUP BY c.id
        ORDER BY c.name
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_cohort(cur, cohort_id: int) -> dict | None:
    cur.execute("""
        SELECT id, name, description, type, config, created_at, updated_at
        FROM cohorts WHERE id = %s
    """, (cohort_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_cohort_members(cur, cohort_id: int, limit: int = 100,
                       offset: int = 0) -> list[dict]:
    cur.execute("""
        SELECT identifier_type, identifier_value, added_at
        FROM cohort_members
        WHERE cohort_id = %s
        ORDER BY added_at DESC
        LIMIT %s OFFSET %s
    """, (cohort_id, limit, offset))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_cohort(cur, *, name: str, description: str, type: str,
                  config: dict = None) -> int:
    cur.execute("""
        INSERT INTO cohorts (name, description, type, config)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (name, description, type, json.dumps(config or {})))
    return cur.fetchone()[0]


def add_members(cur, cohort_id: int, members: list[tuple[str, str]]) -> int:
    """Add members to a cohort. members: list of (identifier_type, identifier_value)."""
    count = 0
    for id_type, id_value in members:
        cur.execute("""
            INSERT INTO cohort_members (cohort_id, identifier_type, identifier_value)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (cohort_id, id_type, id_value.strip()))
        count += cur.rowcount
    return count


def delete_cohort(cur, cohort_id: int) -> bool:
    cur.execute("DELETE FROM cohorts WHERE id = %s RETURNING id", (cohort_id,))
    return cur.fetchone() is not None


def estimate_device_count(cur, cohort_type: str, value: str) -> int:
    """Estimate how many devices match a cohort definition."""
    if cohort_type == "percentage":
        total = 0
        cur.execute("SELECT COUNT(DISTINCT client_uuid) FROM device_connections")
        total = cur.fetchone()[0]
        try:
            pct = int(value)
        except (ValueError, TypeError):
            pct = 0
        return round(total * pct / 100)
    if cohort_type == "email_pattern":
        cur.execute("""
            SELECT COUNT(DISTINCT client_uuid)
            FROM device_connections
            WHERE email ~* %s
        """, (value,))
        return cur.fetchone()[0]
    return 0
