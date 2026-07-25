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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


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
    return dict(zip(cols, row, strict=False))


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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def autocomplete_superseded(cur, *, plugin_id, is_experiment: bool,
                            target_cohort_id, exclude_id: int = None) -> None:
    """Auto-complète UNIQUEMENT la campagne que la nouvelle remplace, pour permettre
    la coexistence des branches d'expérimentation.

    Portée, par classe (COALESCE(is_experiment,false)) et même plugin :
      - release GÉNÉRALE (is_experiment=false) → supersede TOUTES les campagnes
        non-expé actives du plugin (rollout + canaries), quelle que soit la cohorte,
        + nettoyage legacy (plugin_id IS NULL). Conserve la sémantique historique
        « une nouvelle release remplace les précédentes » et ne touche JAMAIS une expé.
      - EXPÉRIENCE (is_experiment=true) → ne complète que l'expé active
        même-plugin/même-cohorte (re-déploiement d'un bras) ; les autres bras,
        le rollout général et les expés d'autres cohortes sont intacts.
    """
    cur.execute("""
        UPDATE campaigns SET status = 'completed', updated_at = NOW()
        WHERE status IN ('active','paused') AND type = 'plugin_update'
          AND COALESCE(is_experiment, false) = %(is_exp)s
          AND (%(is_exp)s = false OR target_cohort_id IS NOT DISTINCT FROM %(cohort)s)
          AND (plugin_id = %(pid)s OR (%(is_exp)s = false AND plugin_id IS NULL))
          AND (%(exclude)s::int IS NULL OR id <> %(exclude)s)
    """, {"is_exp": is_experiment, "cohort": target_cohort_id,
          "pid": plugin_id, "exclude": exclude_id})


def create_campaign(cur, *, name: str, description: str = "", type: str = "plugin_update",
                    artifact_id: int = None, rollback_artifact_id: int = None,
                    target_cohort_id: int = None, urgency: str = "normal",
                    deadline_at: str = None, status: str = "draft",
                    rollout_config: dict = None,
                    created_by: str = None,
                    plugin_id: int = None,
                    is_experiment: bool = False, priority: int = 0) -> int:
    # Auto-complete only the campaign this one supersedes (scoped), so experiment
    # arms and the general rollout can coexist. See autocomplete_superseded.
    if status == "active":
        autocomplete_superseded(cur, plugin_id=plugin_id, is_experiment=is_experiment,
                                target_cohort_id=target_cohort_id)
    cur.execute("""
        INSERT INTO campaigns (name, description, type, artifact_id,
                              rollback_artifact_id, target_cohort_id,
                              urgency, deadline_at, status, rollout_config, created_by,
                              plugin_id, is_experiment, priority)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (name, description, type, artifact_id, rollback_artifact_id,
          target_cohort_id, urgency, deadline_at or None, status,
          json.dumps(rollout_config) if rollout_config else None,
          created_by, plugin_id, is_experiment, priority))
    return cur.fetchone()[0]


def update_campaign_status(cur, campaign_id: int, new_status: str) -> bool:
    """Update campaign status. Returns True if updated.

    À l'activation, applique l'auto-complétion scopée (les transitions manuelles
    activate/resume ne passent pas par create_campaign) : sinon activer un rollout
    général via draft→active laisserait 2 gagnants généraux non déterministes.
    """
    if new_status == "active":
        cur.execute("""
            SELECT plugin_id, COALESCE(is_experiment, false), target_cohort_id
            FROM campaigns WHERE id = %s AND type = 'plugin_update'
        """, (campaign_id,))
        row = cur.fetchone()
        if row:
            autocomplete_superseded(cur, plugin_id=row[0], is_experiment=row[1],
                                    target_cohort_id=row[2], exclude_id=campaign_id)
    cur.execute("""
        UPDATE campaigns SET status = %s, updated_at = NOW()
        WHERE id = %s
        RETURNING id
    """, (new_status, campaign_id))
    return cur.fetchone() is not None
