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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_cohort(cur, cohort_id: int) -> dict | None:
    cur.execute("""
        SELECT id, name, description, type, config, created_at, updated_at
        FROM cohorts WHERE id = %s
    """, (cohort_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


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


class TargetError(ValueError):
    """Cible de déploiement refusée : elle ne désigne personne, ou pas ce qui est demandé."""


def validate_deploy_target(cur, *, target_mode: str, cohort_id="", percent="",
                           emails: str = "", experiment: bool = False) -> tuple:
    """Cible demandée, contrôlée sans rien créer.

    Renvoie ("all",), ("cohort", id), ("percent", n) ou ("emails", [adresses]).
    Une cible incomplète est REFUSÉE (TargetError) : avant l'issue #132,
    « cohorte existante » sans cohorte et une liste d'adresses vide retombaient
    en silence sur tout le parc.

    `experiment` : une campagne d'expérimentation épingle sa version, y compris
    en arrière, et coexiste avec le rollout général. Elle exige une cible
    délimitée — cohorte manuelle, motif d'adresse, groupe Keycloak ou liste
    d'adresses — jamais tout le parc ni un pourcentage (deux cohortes en
    pourcentage se recouvrent au lieu de partitionner).
    """
    mode = (target_mode or "all").strip()
    if mode == "all":
        if experiment:
            raise TargetError("Une campagne d'expérimentation vise une cohorte ou une liste d'adresses, "
                              "pas tout le parc. Pour tout le parc, choisissez une campagne ordinaire.")
        return ("all",)
    if mode == "existing":
        raw = str(cohort_id or "").strip()
        if not raw:
            raise TargetError("Cible « cohorte existante » : choisissez une cohorte"
                              + ("." if experiment else ", ou sélectionnez « Tous les utilisateurs »."))
        try:
            cid = int(raw)
        except ValueError as e:
            raise TargetError("Cohorte invalide.") from e
        cohort = get_cohort(cur, cid)
        if cohort is None:
            raise TargetError("Cohorte introuvable.")
        if experiment and cohort["type"] == "percentage":
            raise TargetError("Une campagne d'expérimentation ne peut pas viser une cohorte en "
                              "pourcentage : choisissez une cohorte manuelle, par motif d'adresse "
                              "ou par groupe Keycloak.")
        return ("cohort", cid)
    if mode == "percent":
        if experiment:
            raise TargetError("Une campagne d'expérimentation ne peut pas viser un pourcentage de "
                              "comptes : choisissez une cohorte ou une liste d'adresses.")
        try:
            pct = int(str(percent).strip())
        except ValueError as e:
            raise TargetError("Cible « pourcentage » : indiquez un nombre entre 1 et 100.") from e
        if not 1 <= pct <= 100:
            raise TargetError("Cible « pourcentage » : indiquez un nombre entre 1 et 100.")
        return ("percent", pct)
    if mode == "emails":
        email_list = [e.strip() for e in str(emails or "").splitlines() if e.strip()]
        if not email_list:
            raise TargetError("Cible « adresses email » : la liste est vide. Saisissez au moins une adresse"
                              + ("." if experiment else ", ou sélectionnez « Tous les utilisateurs »."))
        return ("emails", email_list)
    raise TargetError(f"Cible inconnue : {mode}.")


def resolve_deploy_target(cur, *, target_mode: str, cohort_id="", percent="",
                          emails: str = "", auto_name: str, auto_description: str = "",
                          experiment: bool = False) -> int | None:
    """Cohorte visée par un déploiement, ou None pour « tout le parc ».

    Implémentation unique pour les formulaires de déploiement : contrôle
    (validate_deploy_target) puis création de la cohorte d'office s'il en faut
    une. `auto_name` : préfixe de son nom (suffixé par `-<n>pct` ou `-manual`).
    La cohorte « pourcentage » porte son pourcentage sous la clé `percentage`,
    celle que lit app.main._resolve_device_cohorts (créée sans, elle ne
    contenait personne).
    """
    spec = validate_deploy_target(cur, target_mode=target_mode, cohort_id=cohort_id,
                                  percent=percent, emails=emails, experiment=experiment)
    if spec[0] == "all":
        return None
    if spec[0] == "cohort":
        return spec[1]
    if spec[0] == "percent":
        pct = spec[1]
        return create_cohort(cur, name=f"{auto_name}-{pct}pct",
                             description=auto_description or f"Auto: {pct}%",
                             type="percentage", config={"percentage": pct})
    email_list = spec[1]
    cid = create_cohort(cur, name=f"{auto_name}-manual",
                        description=auto_description or f"Auto: {len(email_list)} emails",
                        type="manual")
    add_members(cur, cid, [("email", e) for e in email_list])
    return cid


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
