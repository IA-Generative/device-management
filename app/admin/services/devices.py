"""Device service — queries for device list, detail, health, telemetry."""

from __future__ import annotations

from datetime import datetime, timezone


def list_devices(cur, *, owner: str = None, platform: str = None,
                 health: str = None, enrollment: str = None,
                 limit: int = 50, offset: int = 0) -> list[dict]:
    """List devices with search/filter, computing health status."""
    conditions = []
    params = []
    having = []

    if owner:
        conditions.append(
            "(dc.email ILIKE %s OR dc.user_agent ILIKE %s)"
        )
        params.extend([f"%{owner}%", f"%{owner}%"])
    if platform:
        conditions.append("dc.action = %s")
        params.append(platform)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    if health:
        having.append("""
            CASE
                WHEN MAX(dc.created_at) IS NULL THEN 'never'
                WHEN NOW() - MAX(dc.created_at) > INTERVAL '24 hours' THEN 'stale'
                ELSE 'ok'
            END = %s
        """)
        params.append(health)

    having_clause = "HAVING " + " AND ".join(having) if having else ""
    params.extend([limit, offset])

    cur.execute(f"""
        SELECT
            dc.client_uuid::text,
            dc.email,
            dc.action AS platform_type,
            MAX(dc.user_agent) AS user_agent,
            MAX(dc.created_at) AS last_contact,
            p.status AS enrollment_status,
            CASE
                WHEN MAX(dc.created_at) IS NULL THEN 'never'
                WHEN NOW() - MAX(dc.created_at) > INTERVAL '24 hours' THEN 'stale'
                ELSE 'ok'
            END AS health
        FROM device_connections dc
        LEFT JOIN provisioning p ON p.client_uuid = dc.client_uuid
            AND p.status IN ('PENDING', 'ENROLLED')
        {where}
        GROUP BY dc.client_uuid, dc.email, dc.action, p.status
        {having_clause}
        ORDER BY MAX(dc.created_at) DESC NULLS LAST
        LIMIT %s OFFSET %s
    """, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def count_devices(cur) -> int:
    cur.execute("SELECT COUNT(DISTINCT client_uuid) FROM device_connections")
    return cur.fetchone()[0]


def health_summary(cur) -> dict:
    """Return device health summary counts."""
    cur.execute("""
        SELECT
            COUNT(*) FILTER (
                WHERE NOW() - last_contact <= INTERVAL '24 hours'
            ) AS ok_count,
            COUNT(*) FILTER (
                WHERE NOW() - last_contact > INTERVAL '24 hours'
                  AND NOW() - last_contact <= INTERVAL '7 days'
            ) AS stale_count,
            COUNT(*) FILTER (
                WHERE last_contact IS NULL
            ) AS never_count
        FROM (
            SELECT client_uuid, MAX(created_at) AS last_contact
            FROM device_connections
            GROUP BY client_uuid
        ) sub
    """)
    row = cur.fetchone()
    return {
        "ok_count": row[0] or 0,
        "stale_count": row[1] or 0,
        "error_count": 0,  # Needs error tracking in device_connections
        "never_count": row[2] or 0,
    }


def get_device_detail(cur, client_uuid: str) -> dict | None:
    """Get device detail info."""
    cur.execute("""
        SELECT
            dc.client_uuid::text,
            dc.email,
            dc.action AS platform_type,
            dc.user_agent,
            dc.source_ip::text,
            dc.created_at AS last_contact,
            p.status AS enrollment_status,
            p.device_name
        FROM device_connections dc
        LEFT JOIN provisioning p ON p.client_uuid = dc.client_uuid
            AND p.status IN ('PENDING', 'ENROLLED')
        WHERE dc.client_uuid::text = %s
        ORDER BY dc.created_at DESC
        LIMIT 1
    """, (client_uuid,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_device_connections(cur, client_uuid: str, limit: int = 20) -> list[dict]:
    """Get recent connections for a device."""
    cur.execute("""
        SELECT created_at, source_ip::text, user_agent, action,
               encryption_key_fingerprint
        FROM device_connections
        WHERE client_uuid::text = %s
        ORDER BY created_at DESC
        LIMIT %s
    """, (client_uuid, limit))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_device_activity(cur, client_uuid: str, limit: int = 50) -> list[dict]:
    """Get recent telemetry events for a device."""
    cur.execute("""
        SELECT span_name, span_ts, attributes, plugin_version, received_at
        FROM device_telemetry_events
        WHERE client_uuid = %s
        ORDER BY received_at DESC
        LIMIT %s
    """, (client_uuid, limit))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_device_campaign_statuses(cur, client_uuid: str) -> list[dict]:
    """Get campaign statuses for a device."""
    cur.execute("""
        SELECT cds.campaign_id, c.name AS campaign_name, c.status AS campaign_status,
               cds.status AS device_status, cds.version_before, cds.version_after,
               cds.error_message, cds.updated_at
        FROM campaign_device_status cds
        JOIN campaigns c ON c.id = cds.campaign_id
        WHERE cds.client_uuid = %s
        ORDER BY cds.updated_at DESC
    """, (client_uuid,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_device_flags(cur, client_uuid: str, email: str = None) -> list[dict]:
    """Get effective feature flag values for a device."""
    cur.execute("""
        SELECT ff.id, ff.name, ff.description, ff.default_value,
               ffo.value AS override_value, ffo.min_plugin_version,
               c.name AS cohort_name
        FROM feature_flags ff
        LEFT JOIN feature_flag_overrides ffo ON ffo.feature_id = ff.id
        LEFT JOIN cohorts c ON c.id = ffo.cohort_id
        LEFT JOIN cohort_members cm ON cm.cohort_id = c.id
            AND (
                (cm.identifier_type = 'client_uuid' AND cm.identifier_value = %s)
                OR (cm.identifier_type = 'email' AND cm.identifier_value = %s)
            )
        ORDER BY ff.name
    """, (client_uuid, email or ""))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
