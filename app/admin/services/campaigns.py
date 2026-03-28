"""Campaign service — CRUD and lifecycle operations."""

from __future__ import annotations

import json


def list_campaigns(cur, *, status: str = None, limit: int = 50,
                   offset: int = 0) -> list[dict]:
    conditions = []
    params = []
    if status:
        conditions.append("c.status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    cur.execute(f"""
        SELECT c.id, c.name, c.description, c.type, c.status, c.urgency,
               c.created_at, c.updated_at, c.deadline_at, c.created_by,
               co.name AS cohort_name,
               a.version AS artifact_version, a.device_type,
               ra.version AS rollback_version
        FROM campaigns c
        LEFT JOIN cohorts co ON co.id = c.target_cohort_id
        LEFT JOIN artifacts a ON a.id = c.artifact_id
        LEFT JOIN artifacts ra ON ra.id = c.rollback_artifact_id
        {where}
        ORDER BY c.updated_at DESC
        LIMIT %s OFFSET %s
    """, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_campaign(cur, campaign_id: int) -> dict | None:
    cur.execute("""
        SELECT c.*, co.name AS cohort_name, co.type AS cohort_type,
               a.version AS artifact_version, a.device_type, a.checksum,
               ra.version AS rollback_version
        FROM campaigns c
        LEFT JOIN cohorts co ON co.id = c.target_cohort_id
        LEFT JOIN artifacts a ON a.id = c.artifact_id
        LEFT JOIN artifacts ra ON ra.id = c.rollback_artifact_id
        WHERE c.id = %s
    """, (campaign_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_campaign_stats(cur, campaign_id: int) -> dict:
    """Get campaign progress statistics."""
    cur.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'updated') AS updated,
            COUNT(*) FILTER (WHERE status = 'notified') AS notified,
            COUNT(*) FILTER (WHERE status = 'pending') AS pending,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COUNT(*) FILTER (WHERE status = 'rolled_back') AS rolled_back
        FROM campaign_device_status
        WHERE campaign_id = %s
    """, (campaign_id,))
    row = cur.fetchone()
    total = row[0] or 0
    updated = row[1] or 0
    failed = row[4] or 0
    return {
        "total": total,
        "updated": updated,
        "notified": row[2] or 0,
        "pending": row[3] or 0,
        "failed": failed,
        "rolled_back": row[5] or 0,
        "progress_pct": round(updated / total * 100, 1) if total else 0,
        "error_pct": round(failed / total * 100, 1) if total else 0,
    }


def get_campaign_events(cur, campaign_id: int, limit: int = 20) -> list[dict]:
    """Get recent campaign device events."""
    cur.execute("""
        SELECT client_uuid, email, status, version_before, version_after,
               error_message, updated_at
        FROM campaign_device_status
        WHERE campaign_id = %s
        ORDER BY updated_at DESC
        LIMIT %s
    """, (campaign_id, limit))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_campaign(cur, *, name: str, description: str, type: str,
                    artifact_id: int = None, rollback_artifact_id: int = None,
                    target_cohort_id: int = None, urgency: str = "normal",
                    deadline_at: str = None, status: str = "draft",
                    created_by: str = None) -> int:
    cur.execute("""
        INSERT INTO campaigns (name, description, type, artifact_id,
                              rollback_artifact_id, target_cohort_id,
                              urgency, deadline_at, status, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (name, description, type, artifact_id, rollback_artifact_id,
          target_cohort_id, urgency, deadline_at or None, status, created_by))
    return cur.fetchone()[0]


def update_campaign_status(cur, campaign_id: int, new_status: str) -> bool:
    """Update campaign status. Returns True if updated."""
    cur.execute("""
        UPDATE campaigns SET status = %s, updated_at = NOW()
        WHERE id = %s
        RETURNING id
    """, (new_status, campaign_id))
    return cur.fetchone() is not None
