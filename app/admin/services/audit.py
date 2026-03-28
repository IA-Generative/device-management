"""Audit log service — read operations for the audit log UI."""

from __future__ import annotations


def list_audit_entries(cur, *, actor: str = None, action: str = None,
                       resource_type: str = None, date_from: str = None,
                       date_to: str = None, limit: int = 100, offset: int = 0) -> list[dict]:
    conditions = []
    params = []
    if actor:
        conditions.append("actor_email ILIKE %s")
        params.append(f"%{actor}%")
    if action:
        conditions.append("action ILIKE %s")
        params.append(f"%{action}%")
    if resource_type:
        conditions.append("resource_type = %s")
        params.append(resource_type)
    if date_from:
        conditions.append("created_at >= %s::timestamptz")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= %s::timestamptz")
        params.append(date_to)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    cur.execute(f"""
        SELECT id, created_at, actor_email, actor_sub, action,
               resource_type, resource_id, payload, ip_address, user_agent
        FROM admin_audit_log
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def count_audit_entries(cur, *, actor: str = None, action: str = None,
                        resource_type: str = None, date_from: str = None,
                        date_to: str = None) -> int:
    conditions = []
    params = []
    if actor:
        conditions.append("actor_email ILIKE %s")
        params.append(f"%{actor}%")
    if action:
        conditions.append("action ILIKE %s")
        params.append(f"%{action}%")
    if resource_type:
        conditions.append("resource_type = %s")
        params.append(resource_type)
    if date_from:
        conditions.append("created_at >= %s::timestamptz")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= %s::timestamptz")
        params.append(date_to)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    cur.execute(f"SELECT COUNT(*) FROM admin_audit_log {where}", params)
    return cur.fetchone()[0]
