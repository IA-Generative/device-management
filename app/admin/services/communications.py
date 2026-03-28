"""Communications service — announcements, alerts, surveys, changelogs."""

from __future__ import annotations
import json


def list_communications(cur, *, type: str = None, status: str = None,
                        plugin_id: int = None,
                        limit: int = 50, offset: int = 0) -> list[dict]:
    conditions, params = [], []
    if type:
        conditions.append("c.type = %s"); params.append(type)
    if status:
        conditions.append("c.status = %s"); params.append(status)
    if plugin_id:
        conditions.append("c.target_plugin_id = %s"); params.append(plugin_id)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])
    cur.execute(f"""
        SELECT c.*,
               p.name AS plugin_name,
               co.name AS cohort_name,
               COUNT(DISTINCT ca.client_uuid) AS ack_count,
               COUNT(DISTINCT sr.id) AS response_count
        FROM communications c
        LEFT JOIN plugins p ON p.id = c.target_plugin_id
        LEFT JOIN cohorts co ON co.id = c.target_cohort_id
        LEFT JOIN communication_acks ca ON ca.communication_id = c.id
        LEFT JOIN survey_responses sr ON sr.communication_id = c.id
        {where}
        GROUP BY c.id, p.name, co.name
        ORDER BY c.updated_at DESC
        LIMIT %s OFFSET %s
    """, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_communication(cur, comm_id: int) -> dict | None:
    cur.execute("""
        SELECT c.*, p.name AS plugin_name, co.name AS cohort_name
        FROM communications c
        LEFT JOIN plugins p ON p.id = c.target_plugin_id
        LEFT JOIN cohorts co ON co.id = c.target_cohort_id
        WHERE c.id = %s
    """, (comm_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_communication_stats(cur, comm_id: int) -> dict:
    cur.execute("SELECT COUNT(*) FROM communication_acks WHERE communication_id = %s", (comm_id,))
    ack_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM survey_responses WHERE communication_id = %s", (comm_id,))
    response_count = cur.fetchone()[0]
    return {"ack_count": ack_count, "response_count": response_count}


def get_survey_results(cur, comm_id: int) -> dict:
    """Aggregate survey choices with counts."""
    cur.execute("""
        SELECT choices, comment, email, responded_at
        FROM survey_responses
        WHERE communication_id = %s
        ORDER BY responded_at DESC
    """, (comm_id,))
    cols = [d[0] for d in cur.description]
    responses = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Tally choices
    choice_counts: dict[str, int] = {}
    comments = []
    for r in responses:
        raw = r.get("choices")
        if isinstance(raw, str):
            raw = json.loads(raw)
        for c in (raw or []):
            choice_counts[str(c)] = choice_counts.get(str(c), 0) + 1
        if r.get("comment"):
            comments.append({
                "email": r.get("email", ""),
                "comment": r["comment"],
                "responded_at": r.get("responded_at"),
            })

    return {
        "total_responses": len(responses),
        "choice_counts": choice_counts,
        "comments": comments,
    }


def create_communication(cur, *, type: str, title: str, body: str,
                         priority: str = "normal",
                         target_plugin_id: int = None,
                         target_cohort_id: int = None,
                         target_bundle_id: int = None,
                         min_plugin_version: str = "",
                         max_plugin_version: str = "",
                         starts_at: str = None, expires_at: str = None,
                         survey_question: str = "",
                         survey_choices: list = None,
                         survey_allow_multiple: bool = False,
                         survey_allow_comment: bool = False,
                         status: str = "draft",
                         created_by: str = None) -> int:
    cur.execute("""
        INSERT INTO communications
            (type, title, body, priority, target_plugin_id, target_cohort_id,
             target_bundle_id, min_plugin_version, max_plugin_version,
             starts_at, expires_at, survey_question, survey_choices,
             survey_allow_multiple, survey_allow_comment, status, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (type, title, body, priority,
          target_plugin_id or None, target_cohort_id or None,
          target_bundle_id or None,
          min_plugin_version or None, max_plugin_version or None,
          starts_at or None, expires_at or None,
          survey_question or None,
          json.dumps(survey_choices) if survey_choices else None,
          survey_allow_multiple, survey_allow_comment,
          status, created_by))
    return cur.fetchone()[0]


def update_communication_status(cur, comm_id: int, new_status: str) -> bool:
    cur.execute("""
        UPDATE communications SET status = %s, updated_at = NOW()
        WHERE id = %s RETURNING id
    """, (new_status, comm_id))
    return cur.fetchone() is not None


def get_active_communications(cur, *, plugin_slug: str = None,
                              client_uuid: str = None) -> list[dict]:
    """Get active, non-expired communications for config endpoint."""
    cur.execute("""
        SELECT c.id, c.type, c.title, c.body, c.priority,
               c.starts_at, c.expires_at,
               c.survey_question, c.survey_choices,
               c.survey_allow_multiple, c.survey_allow_comment
        FROM communications c
        LEFT JOIN plugins p ON p.id = c.target_plugin_id
        WHERE c.status = 'active'
          AND (c.starts_at IS NULL OR c.starts_at <= NOW())
          AND (c.expires_at IS NULL OR c.expires_at > NOW())
          AND (p.slug IS NULL OR p.slug = %s OR c.target_plugin_id IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM communication_acks ca
              WHERE ca.communication_id = c.id AND ca.client_uuid = %s
          )
        ORDER BY
            CASE c.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                            WHEN 'normal' THEN 2 ELSE 3 END,
            c.starts_at DESC
        LIMIT 10
    """, (plugin_slug or "", client_uuid or ""))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def ack_communication(cur, comm_id: int, client_uuid: str):
    cur.execute("""
        INSERT INTO communication_acks (communication_id, client_uuid)
        VALUES (%s, %s) ON CONFLICT DO NOTHING
    """, (comm_id, client_uuid))


def submit_survey_response(cur, comm_id: int, client_uuid: str,
                           email: str = "", choices: list = None,
                           comment: str = ""):
    cur.execute("""
        INSERT INTO survey_responses (communication_id, client_uuid, email, choices, comment)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (communication_id, client_uuid) DO UPDATE
        SET choices = %s, comment = %s, responded_at = NOW()
    """, (comm_id, client_uuid, email,
          json.dumps(choices or []), comment,
          json.dumps(choices or []), comment))
