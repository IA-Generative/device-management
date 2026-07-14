"""Feature flags service — CRUD + réconciliation catalogue à l'import.

Le catalogue est SCOPÉ par plugin (`plugin_slug`, ''=global/legacy) et son
`default_value` est INDICATIF (recopié du template.default à l'import) : les
valeurs autoritaires viennent du config template, résolues par profil dans
/config. Un flag disparu du template au bump est marqué `deprecated`
(orphelin), jamais supprimé automatiquement.
"""

from __future__ import annotations


def list_flags(cur) -> list[dict]:
    cur.execute("""
        SELECT ff.id, ff.name, ff.plugin_slug, ff.description, ff.default_value,
               ff.deprecated, ff.min_plugin_version, ff.created_at, ff.updated_at,
               COUNT(ffo.cohort_id) AS override_count
        FROM feature_flags ff
        LEFT JOIN feature_flag_overrides ffo ON ffo.feature_id = ff.id
        GROUP BY ff.id
        ORDER BY ff.plugin_slug, ff.name
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_flag(cur, flag_id: int) -> dict | None:
    cur.execute("""
        SELECT id, name, plugin_slug, description, default_value, deprecated,
               min_plugin_version, created_at, updated_at
        FROM feature_flags WHERE id = %s
    """, (flag_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def create_flag(cur, *, name: str, description: str, default_value: bool,
                plugin_slug: str = "", min_plugin_version: str | None = None) -> int:
    """Crée un flag scopé `plugin_slug` (''=global), applicable à partir de
    `min_plugin_version` (NULL = toutes les versions)."""
    cur.execute("""
        INSERT INTO feature_flags (name, description, default_value, plugin_slug, min_plugin_version)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (name, description, default_value, plugin_slug or "", min_plugin_version or None))
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


def delete_flag(cur, flag_id: int) -> bool:
    """Supprime un flag et ses overrides (miroir de delete_cohort)."""
    cur.execute("DELETE FROM feature_flag_overrides WHERE feature_id = %s", (flag_id,))
    cur.execute("DELETE FROM feature_flags WHERE id = %s RETURNING id", (flag_id,))
    return cur.fetchone() is not None


def reconcile_catalog_from_template(cur, *, plugin_slug: str, template: dict) -> dict:
    """Réconcilie le catalogue scopé `plugin_slug` avec un dm-config.json importé.

    Noms de flags = UNION des clés `featureToggles` sur default + tous les
    profils. UPSERT de chaque flag (default_value indicatif = valeur du
    template.default quand présente, sinon celle du premier profil qui le
    déclare) ; un flag revenu d'orphelin est réactivé ; les flags du catalogue
    absents du template sont MARQUÉS deprecated (pas de delete — un admin les
    supprime explicitement via delete_flag).

    Returns:
        {"added": [...], "kept": [...], "reactivated": [...],
         "orphaned": [...], "already_deprecated": [...]}
    """
    names: dict[str, bool] = {}
    if isinstance(template, dict):
        for section in template.values():
            if isinstance(section, dict) and isinstance(section.get("featureToggles"), dict):
                for key, val in section["featureToggles"].items():
                    names.setdefault(str(key), bool(val))
        default_section = template.get("default")
        if isinstance(default_section, dict) and isinstance(default_section.get("featureToggles"), dict):
            for key, val in default_section["featureToggles"].items():
                names[str(key)] = bool(val)  # le default du template fixe l'indicatif

    cur.execute("SELECT name, deprecated FROM feature_flags WHERE plugin_slug = %s", (plugin_slug,))
    existing = {row[0]: bool(row[1]) for row in cur.fetchall()}

    added, kept, reactivated = [], [], []
    for name in sorted(names):
        if name not in existing:
            added.append(name)
        elif existing[name]:
            reactivated.append(name)
        else:
            kept.append(name)
        cur.execute("""
            INSERT INTO feature_flags (name, plugin_slug, description, default_value, deprecated)
            VALUES (%s, %s, %s, %s, false)
            ON CONFLICT (plugin_slug, name)
            DO UPDATE SET default_value = EXCLUDED.default_value,
                          deprecated = false,
                          updated_at = NOW()
        """, (name, plugin_slug, f"Importé du template {plugin_slug}", names[name]))

    orphaned = sorted(n for n, dep in existing.items() if n not in names and not dep)
    already_deprecated = sorted(n for n, dep in existing.items() if n not in names and dep)
    if orphaned:
        cur.execute("""
            UPDATE feature_flags SET deprecated = true, updated_at = NOW()
            WHERE plugin_slug = %s AND name = ANY(%s)
        """, (plugin_slug, orphaned))

    return {"added": added, "kept": kept, "reactivated": reactivated,
            "orphaned": orphaned, "already_deprecated": already_deprecated}
