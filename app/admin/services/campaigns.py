"""Campaign service — CRUD and lifecycle operations."""

from __future__ import annotations

import json
import re

from ...services import update_rules


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
               -- Sans ces deux champs, la liste montre deux campagnes « actives »
               -- sur le même plugin sans dire laquelle l'emporte : la précédence
               -- (bras ciblé > général, puis priority) resterait illisible.
               COALESCE(c.is_experiment, false) AS is_experiment, c.priority,
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
                            target_cohort_id, exclude_id: int = None,
                            campaign_type: str = "plugin_update") -> None:
    """Auto-complète UNIQUEMENT la campagne que la nouvelle remplace, pour permettre
    la coexistence des branches d'expérimentation.

    **Implémentation unique de cette règle** : tous les chemins qui activent une
    campagne l'appellent (création admin, création API, deploy plugin, transitions
    manuelles activate/resume). Ne pas réécrire l'UPDATE ailleurs — la précédence
    des campagnes doit rester modifiable en un seul endroit.

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
        WHERE status IN ('active','paused') AND type = %(type)s
          AND COALESCE(is_experiment, false) = %(is_exp)s
          AND (%(is_exp)s = false OR target_cohort_id IS NOT DISTINCT FROM %(cohort)s)
          AND (plugin_id = %(pid)s OR (%(is_exp)s = false AND plugin_id IS NULL))
          AND (%(exclude)s::int IS NULL OR id <> %(exclude)s)
    """, {"type": campaign_type, "is_exp": is_experiment, "cohort": target_cohort_id,
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


# ── Qui cette campagne sert-elle ? (issue #132) ──────────────────────────────

_REASON_LABELS = {
    update_rules.UP_TO_DATE: "déjà sur la version cible",
    update_rules.AHEAD: "sur une version postérieure à la cible (pas de rollback configuré)",
    update_rules.NO_VERSION: "version installée inconnue",
    update_rules.NO_TARGET: "campagne sans version cible",
    "outside_cohort": "hors de la cohorte visée",
    "rollout_waiting": "pas encore dans le palier en cours",
}


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _cohort_matcher(cur, cohort_id, cohort_type, cohort_config):
    """(fonction (client_uuid, email) -> bool, note) pour la cohorte visée.

    Mêmes règles que app.main._resolve_device_cohorts. Le groupe Keycloak n'est
    pas évalué ici (il faudrait interroger Keycloak) : tous les postes sont
    alors comptés, et la note le dit.
    """
    if cohort_id is None:
        return (lambda _u, _e: True), None
    config = _as_dict(cohort_config)
    if cohort_type == "manual":
        cur.execute("SELECT identifier_type, identifier_value FROM cohort_members WHERE cohort_id = %s",
                    (cohort_id,))
        emails, uuids = set(), set()
        for id_type, id_value in cur.fetchall():
            (emails if id_type == "email" else uuids).add(str(id_value))
        return (lambda u, e: (e or "") in emails or (u or "") in uuids), None
    if cohort_type == "percentage":
        try:
            pct = int(config.get("percentage", 0))
        except (TypeError, ValueError):
            pct = 0
        note = None
        if pct <= 0:
            note = "La cohorte « pourcentage » visée n'a pas de pourcentage : elle ne contient personne."
        return (lambda u, _e: update_rules.percentage_cohort_admits(u or "", pct)), note
    if cohort_type == "email_pattern":
        pattern = str(config.get("pattern", ""))
        try:
            rx = re.compile(pattern, re.IGNORECASE) if pattern else None
        except re.error:
            rx = None
        if rx is None:
            return (lambda _u, _e: False), "Le motif d'adresse de la cohorte visée est vide ou invalide."
        return (lambda _u, e: bool(rx.match(e or ""))), None
    return ((lambda _u, _e: True),
            "Cohorte de type groupe Keycloak : l'appartenance n'est pas évaluée ici, "
            "tous les postes du plugin sont comptés.")


def campaign_reach(cur, campaign_id: int) -> dict | None:
    """Postes à qui la campagne servirait une directive AUJOURD'HUI, et pourquoi pas les autres.

    Parcourt les installations actives du plugin (plugin_installations : UUID,
    adresse et version vues au dernier appel /config) et applique les règles de
    /config (app.services.update_rules) : cohorte, version, palier.

    Limites, rappelées dans `notes` : la version de l'hôte n'est pas mémorisée,
    les bornes min/max d'hôte ne sont donc pas évaluées ; la précédence entre
    campagnes actives (un bras ciblé l'emporte sur le rollout général) non plus.
    None si la campagne n'existe pas.
    """
    cur.execute("""
        SELECT c.status, c.plugin_id, c.target_cohort_id, COALESCE(c.is_experiment, false),
               c.rollout_config, c.created_at,
               a.version, a.device_type, a.min_host_version, a.max_host_version,
               ra.version, co.name, co.type, co.config
        FROM campaigns c
        LEFT JOIN artifacts a  ON a.id  = c.artifact_id
        LEFT JOIN artifacts ra ON ra.id = c.rollback_artifact_id
        LEFT JOIN cohorts co   ON co.id = c.target_cohort_id
        WHERE c.id = %s
    """, (campaign_id,))
    row = cur.fetchone()
    if not row:
        return None
    (status, plugin_id, cohort_id, is_experiment, rollout_config, created_at,
     target_version, device_type, min_host, max_host,
     rollback_version, cohort_name, cohort_type, cohort_config) = row
    target_version = str(target_version or "")

    if plugin_id is not None:
        cur.execute("""
            SELECT client_uuid, email, installed_version FROM plugin_installations
            WHERE plugin_id = %s AND status = 'active'
        """, (plugin_id,))
    else:
        # Campagne legacy sans plugin : /config la rattache par le type d'artefact.
        cur.execute("""
            SELECT pi.client_uuid, pi.email, pi.installed_version
            FROM plugin_installations pi JOIN plugins p ON p.id = pi.plugin_id
            WHERE p.device_type = %s AND pi.status = 'active'
        """, (device_type,))
    devices = cur.fetchall()

    in_cohort, cohort_note = _cohort_matcher(cur, cohort_id, cohort_type, cohort_config)
    percent = update_rules.campaign_rollout_percent(_as_dict(rollout_config) or None, created_at)

    reasons: dict[str, int] = {}
    eligible = rollbacks = 0
    for client_uuid, email, installed_version in devices:
        if not in_cohort(client_uuid, email):
            reason = "outside_cohort"
        else:
            action, reason = update_rules.decide_update(
                plugin_version=installed_version or "", target_version=target_version,
                is_experiment=bool(is_experiment), has_rollback=bool(rollback_version))
            if action is not None:
                if update_rules.rollout_admits(client_uuid or "", percent):
                    eligible += 1
                    rollbacks += action == update_rules.ROLLBACK
                    continue
                reason = "rollout_waiting"
        reasons[reason] = reasons.get(reason, 0) + 1

    notes = []
    if status != "active":
        notes.append(f"Campagne « {status} » : rien n'est servi tant qu'elle n'est pas active.")
    if cohort_note:
        notes.append(cohort_note)
    if min_host or max_host:
        notes.append(f"Bornes de version d'hôte (min {min_host or '-'}, max {max_host or '-'}) non "
                     "évaluées : la version de l'hôte n'est pas mémorisée par poste.")

    zero_reason = None
    if eligible == 0:
        if not target_version:
            zero_reason = "La campagne n'a pas d'artefact : elle ne peut rien servir."
        elif not devices:
            zero_reason = "Aucune installation active connue pour ce plugin."
        else:
            top = max(reasons.items(), key=lambda kv: kv[1])
            zero_reason = (f"Aucun poste ne recevrait la version {target_version} : "
                           + " ; ".join(f"{n} {_REASON_LABELS.get(r, r)}"
                                        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])))
            if top[0] == "outside_cohort" and top[1] == len(devices):
                zero_reason = (f"La cohorte « {cohort_name or cohort_id} » ne contient aucun poste "
                               "de ce plugin.")
    return {
        "status": status,
        "target_version": target_version,
        "is_experiment": bool(is_experiment),
        "installed": len(devices),
        "eligible": eligible,
        "rollbacks": rollbacks,
        "reasons": reasons,
        "reason_labels": {r: _REASON_LABELS.get(r, r) for r in reasons},
        "rollout_percent": percent,
        "cohort": cohort_name,
        "zero_reason": zero_reason,
        "notes": notes,
    }


# ── La version générale est-elle portée par une campagne ? (issue #132) ──────

def general_version_coverage(cur, plugin_id: int) -> dict | None:
    """État de diffusion par /config de la version générale d'un plugin.

    La version générale n'alimente que les canaux natifs. Les plugins qui ne
    lisent que /config (Thunderbird, …) ne reçoivent une version que par une
    campagne `plugin_update` active. Renvoie None sans version générale, sinon
    {version_id, version, coverage, can_create} avec coverage ∈
      - "full"    : une campagne active non ciblée porte cette version ;
      - "partial" : seulement des campagnes ciblées (cohorte) ;
      - "none"    : aucune campagne active ne la porte.
    """
    cur.execute("""
        SELECT pv.id, pv.version, pv.artifact_id, p.device_type
        FROM plugins p JOIN plugin_versions pv ON pv.id = p.general_version_id
        WHERE p.id = %s
    """, (plugin_id,))
    row = cur.fetchone()
    if not row:
        return None
    version_id, version, artifact_id, device_type = row
    cur.execute("""
        SELECT c.target_cohort_id
        FROM campaigns c LEFT JOIN artifacts a ON a.id = c.artifact_id
        WHERE c.status = 'active' AND c.type = 'plugin_update'
          AND (c.plugin_id = %(pid)s OR (c.plugin_id IS NULL AND a.device_type = %(dt)s))
          AND (c.version_id = %(vid)s OR a.version = %(version)s)
    """, {"pid": plugin_id, "dt": device_type, "vid": version_id, "version": version})
    targets = [r[0] for r in cur.fetchall()]
    if any(t is None for t in targets):
        coverage = "full"
    elif targets:
        coverage = "partial"
    else:
        coverage = "none"
    return {"version_id": version_id, "version": version, "coverage": coverage,
            "can_create": artifact_id is not None}


class DefaultCampaignError(ValueError):
    """La campagne par défaut ne peut pas être créée pour cette version."""


def create_default_campaign(cur, *, plugin_id: int, version_id: int,
                            created_by: str = None) -> int:
    """Campagne par défaut d'une version : active, tout le parc, sans palier.

    C'est la campagne que « Publier et déployer » aurait créée avec la cible
    « Tous les utilisateurs » et la stratégie « Patch all ». Comme toute release
    générale, elle clôt la campagne ordinaire active précédente du plugin
    (autocomplete_superseded) et laisse les expérimentations en place.
    """
    cur.execute("""
        SELECT pv.version, pv.status, pv.artifact_id, p.name
        FROM plugin_versions pv JOIN plugins p ON p.id = pv.plugin_id
        WHERE pv.id = %s AND pv.plugin_id = %s
    """, (version_id, plugin_id))
    row = cur.fetchone()
    if not row:
        raise DefaultCampaignError("Version introuvable pour ce plugin.")
    version, status, artifact_id, plugin_name = row
    if status not in ("published", "deprecated"):
        raise DefaultCampaignError(f"Une version « {status} » ne peut pas être déployée à tout le parc.")
    if artifact_id is None:
        raise DefaultCampaignError("Cette version n'a pas de binaire hébergé : une campagne ne peut pas la servir.")
    campaign_id = create_campaign(
        cur, name=f"MaJ {plugin_name} {version} [tout le parc]", type="plugin_update",
        artifact_id=artifact_id, target_cohort_id=None, status="active",
        created_by=created_by, plugin_id=plugin_id)
    cur.execute("UPDATE campaigns SET version_id = %s WHERE id = %s", (version_id, campaign_id))
    return campaign_id


def list_campaigns_api(cur, *, plugin_slug: str = None, status: str = None,
                       limit: int = 100) -> list[dict]:
    """Campagnes pour GET /api/campaigns : de quoi diagnostiquer sans la base."""
    conditions, params = [], []
    if plugin_slug:
        conditions.append("p.slug = %s")
        params.append(plugin_slug)
    if status:
        conditions.append("c.status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(max(1, min(int(limit), 500)))
    cur.execute(f"""
        SELECT c.id, c.name, c.type, c.status, c.environment,
               c.plugin_id, p.slug AS plugin_slug,
               COALESCE(pv.version, a.version) AS version,
               a.device_type, a.checksum,
               COALESCE(c.is_experiment, false) AS is_experiment, c.priority,
               c.target_cohort_id, co.name AS target_cohort_name, co.type AS target_cohort_type,
               c.rollout_config, c.urgency,
               ra.version AS rollback_version,
               c.created_at, c.updated_at, c.deadline_at, c.created_by
        FROM campaigns c
        LEFT JOIN plugins p ON p.id = c.plugin_id
        LEFT JOIN plugin_versions pv ON pv.id = c.version_id
        LEFT JOIN artifacts a ON a.id = c.artifact_id
        LEFT JOIN artifacts ra ON ra.id = c.rollback_artifact_id
        LEFT JOIN cohorts co ON co.id = c.target_cohort_id
        {where}
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT %s
    """, params)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        item = dict(zip(cols, row, strict=False))
        for key in ("created_at", "updated_at", "deadline_at"):
            if item[key] is not None and hasattr(item[key], "isoformat"):
                item[key] = item[key].isoformat()
        item["rollout_config"] = _as_dict(item["rollout_config"]) or None
        out.append(item)
    return out
