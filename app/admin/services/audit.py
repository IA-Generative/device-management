"""Audit log service — read operations for the audit log UI."""

from __future__ import annotations


def list_audit_entries(cur, *, actor: str = None, action: str = None,
                       resource_type: str = None, date_from: str = None,
                       date_to: str = None, q: str = None,
                       limit: int = 100, offset: int = 0) -> list[dict]:
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
    if q:
        # Recherche plein-texte : détails (payload JSON) + id de ressource
        conditions.append("(payload::text ILIKE %s OR resource_id ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])

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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_audit_facets(cur) -> dict:
    """Valeurs distinctes RÉELLES pour les filtres (datalists d'autocomplétion
    et select des ressources) — dynamiques, jamais codées en dur."""
    facets: dict[str, list[str]] = {}
    for key, column in (("actors", "actor_email"),
                        ("actions", "action"),
                        ("resource_types", "resource_type")):
        cur.execute(f"""
            SELECT DISTINCT {column} FROM admin_audit_log
            WHERE {column} IS NOT NULL AND {column} <> ''
            ORDER BY 1 LIMIT 200
        """)
        facets[key] = [r[0] for r in cur.fetchall()]
    return facets
