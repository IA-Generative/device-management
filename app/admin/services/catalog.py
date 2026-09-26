"""Catalog service — plugins, versions, bundles, installations."""

from __future__ import annotations

import json

# ─── Plugins ──────────────────────────────────────────────────────────

def list_plugins(cur, *, status: str = None, device_type: str = None,
                 category: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
    conditions, params = [], []
    if status:
        conditions.append("p.status = %s")
        params.append(status)
    if device_type:
        conditions.append("p.device_type = %s")
        params.append(device_type)
    if category:
        conditions.append("p.category = %s")
        params.append(category)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])
    cur.execute(f"""
        SELECT p.*,
               COUNT(DISTINCT pv.id) FILTER (WHERE pv.status = 'published') AS version_count,
               MAX(pv.version) FILTER (WHERE pv.status = 'published') AS latest_version,
               COUNT(DISTINCT pi.client_uuid) FILTER (WHERE pi.status = 'active') AS install_count
        FROM plugins p
        LEFT JOIN plugin_versions pv ON pv.plugin_id = p.id
        LEFT JOIN plugin_installations pi ON pi.plugin_id = p.id
        {where}
        GROUP BY p.id
        ORDER BY p.name
        LIMIT %s OFFSET %s
    """, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_plugin(cur, plugin_id: int) -> dict | None:
    cur.execute("SELECT * FROM plugins WHERE id = %s", (plugin_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


def get_plugin_by_slug(cur, slug: str) -> dict | None:
    cur.execute("SELECT * FROM plugins WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


def create_plugin(cur, *, slug: str, name: str, description: str = "",
                  intent: str = "", key_features: list = None,
                  changelog: str = "", device_type: str = "libreoffice",
                  category: str = "productivity", icon_url: str = "",
                  homepage_url: str = "", doc_url: str = "",
                  support_email: str = "", license: str = "",
                  publisher: str = "DNUM", visibility: str = "public",
                  config_template: dict = None) -> int:
    # Disambiguate slug if a removed plugin already holds it
    cur.execute(
        "SELECT id FROM plugins WHERE slug = %s AND status = 'removed'", (slug,)
    )
    old = cur.fetchone()
    if old:
        import time as _t
        cur.execute(
            "UPDATE plugins SET slug = %s WHERE id = %s",
            (f"{slug}__removed_{int(_t.time())}", old[0]),
        )
    cur.execute("""
        INSERT INTO plugins (slug, name, description, intent, key_features, changelog,
                            device_type, category, icon_url, homepage_url, doc_url,
                            support_email, license, publisher, visibility, status, config_template)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)
        RETURNING id
    """, (slug, name, description, intent,
          json.dumps(key_features or []), changelog,
          device_type, category, icon_url, homepage_url, doc_url,
          support_email, license, publisher, visibility,
          json.dumps(config_template) if config_template else None))
    return cur.fetchone()[0]


def update_plugin(cur, plugin_id: int, **fields) -> bool:
    allowed = {"name", "description", "intent", "key_features", "changelog",
               "category", "icon_url", "icon_path", "homepage_url", "support_email",
               "doc_url", "license", "extension_id",
               "publisher", "visibility", "status", "config_template"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            if k == "key_features":
                v = json.dumps(v) if isinstance(v, list) else v
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    params.append(plugin_id)
    cur.execute(f"UPDATE plugins SET {', '.join(sets)} WHERE id = %s RETURNING id", params)
    return cur.fetchone() is not None


def purge_removed(cur) -> int:
    """Permanently DELETE all plugins with status='removed' (cascades to aliases, versions, campaigns, etc.)."""
    cur.execute("SELECT id FROM plugins WHERE status = 'removed'")
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return 0
    cur.execute("DELETE FROM campaigns WHERE plugin_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM plugins WHERE id = ANY(%s)", (ids,))
    return cur.rowcount


# ─── Versions ─────────────────────────────────────────────────────────

def list_versions(cur, plugin_id: int) -> list[dict]:
    cur.execute("""
        SELECT pv.*,
               a.s3_path, a.checksum, a.device_type AS artifact_device_type,
               COUNT(pi.id) FILTER (WHERE pi.installed_version = pv.version) AS install_count
        FROM plugin_versions pv
        LEFT JOIN artifacts a ON a.id = pv.artifact_id
        LEFT JOIN plugin_installations pi ON pi.plugin_id = pv.plugin_id
        WHERE pv.plugin_id = %s
        GROUP BY pv.id, a.s3_path, a.checksum, a.device_type
        ORDER BY pv.created_at DESC
    """, (plugin_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_version(cur, version_id: int) -> dict | None:
    cur.execute("""
        SELECT pv.*, a.s3_path, a.checksum
        FROM plugin_versions pv
        LEFT JOIN artifacts a ON a.id = pv.artifact_id
        WHERE pv.id = %s
    """, (version_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


def create_version(cur, *, plugin_id: int, version: str, artifact_id: int = None,
                   release_notes: str = "", download_url: str = "",
                   min_host_version: str = "", max_host_version: str = "",
                   distribution_mode: str = "managed",
                   status: str = "draft",
                   tag: str = "", hypotheses: list = None) -> int:
    # L'upsert ci-dessous écrase le statut d'une version existante : re-créer la
    # version générale en brouillon ou en expérimentale rendrait tous les canaux
    # natifs muets (issue #40). Même garde que update_version_status.
    if status not in GENERAL_VERSION_STATUSES:
        cur.execute("""
            SELECT 1 FROM plugins p JOIN plugin_versions pv ON pv.id = p.general_version_id
            WHERE p.id = %s AND pv.version = %s
        """, (plugin_id, version))
        if cur.fetchone():
            raise GeneralVersionError(
                f"La version {version} est la version générale : impossible de la "
                f"re-créer en « {status} ». Choisissez d'abord une autre version générale.")
    # Deprecate older published versions when publishing a new one. Une version
    # 'experimental' ne déprécie RIEN : elle coexiste avec la main (pull opt-in).
    if status == "published":
        cur.execute("""
            UPDATE plugin_versions SET status = 'deprecated'
            WHERE plugin_id = %s AND status = 'published' AND version <> %s
        """, (plugin_id, version))
    hyp_json = json.dumps(hypotheses) if hypotheses else None
    # published_at est aussi horodaté pour 'experimental' : le catalogue ordonne
    # les branches par published_at DESC (dernière build d'un tag servie en 1er).
    cur.execute("""
        INSERT INTO plugin_versions
            (plugin_id, version, artifact_id, release_notes, download_url,
             min_host_version, max_host_version, distribution_mode, status,
             tag, hypotheses, published_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                CASE WHEN %s IN ('published','experimental') THEN NOW() ELSE NULL END)
        ON CONFLICT (plugin_id, version) DO UPDATE SET
            artifact_id = COALESCE(EXCLUDED.artifact_id, plugin_versions.artifact_id),
            release_notes = CASE WHEN EXCLUDED.release_notes <> '' THEN EXCLUDED.release_notes ELSE plugin_versions.release_notes END,
            download_url = COALESCE(EXCLUDED.download_url, plugin_versions.download_url),
            min_host_version = COALESCE(EXCLUDED.min_host_version, plugin_versions.min_host_version),
            max_host_version = COALESCE(EXCLUDED.max_host_version, plugin_versions.max_host_version),
            distribution_mode = EXCLUDED.distribution_mode,
            status = EXCLUDED.status,
            tag = COALESCE(EXCLUDED.tag, plugin_versions.tag),
            hypotheses = COALESCE(EXCLUDED.hypotheses, plugin_versions.hypotheses),
            published_at = CASE WHEN EXCLUDED.status IN ('published','experimental') THEN COALESCE(plugin_versions.published_at, NOW()) ELSE plugin_versions.published_at END
        RETURNING id
    """, (plugin_id, version, artifact_id or None, release_notes,
          download_url or None, min_host_version or None,
          max_host_version or None, distribution_mode, status,
          tag or None, hyp_json, status))
    return cur.fetchone()[0]


def link_version_artifact(cur, *, plugin_version_id: int, artifact_id: int,
                          platform_variant: str) -> int:
    """Upsert le lien 1:N version→artefact pour une variante donnée.

    Permet à une release de porter plusieurs binaires (ex. .crx Chromium +
    .xpi Gecko) × cibles, désambiguïsés par platform_variant. Idempotent :
    un nouvel upload de la même variante met à jour l'artefact pointé.
    """
    cur.execute("""
        INSERT INTO plugin_version_artifacts (plugin_version_id, artifact_id, platform_variant)
        VALUES (%s, %s, %s)
        ON CONFLICT (plugin_version_id, platform_variant) DO UPDATE SET
            artifact_id = EXCLUDED.artifact_id
        RETURNING id
    """, (plugin_version_id, artifact_id, platform_variant))
    return cur.fetchone()[0]


def get_version_artifacts(cur, plugin_version_id: int) -> list[dict]:
    """Liste les artefacts (variantes) liés à une version, avec leur chemin/checksum."""
    cur.execute("""
        SELECT pva.platform_variant, a.id AS artifact_id, a.s3_path, a.checksum,
               a.device_type, a.version
        FROM plugin_version_artifacts pva
        JOIN artifacts a ON a.id = pva.artifact_id
        WHERE pva.plugin_version_id = %s
        ORDER BY pva.platform_variant
    """, (plugin_version_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


VERSION_STATUSES = ("draft", "published", "deprecated", "yanked", "experimental")


class GeneralVersionError(ValueError):
    """Opération refusée parce qu'elle viserait ou casserait la version générale."""


def update_version_status(cur, version_id: int, new_status: str,
                          plugin_id: int | None = None) -> bool:
    """Change le statut d'une version.

    `plugin_id` fourni : la version doit appartenir à ce plugin. La version
    générale (issue #40) ne peut passer qu'en `published` ou `deprecated` :
    la retirer (`yanked`) ou la repasser en brouillon rendrait tous les canaux
    natifs muets — choisir d'abord une autre générale.
    """
    if new_status not in VERSION_STATUSES:
        raise ValueError(f"statut inconnu : {new_status}")
    if new_status not in GENERAL_VERSION_STATUSES:
        cur.execute("SELECT 1 FROM plugins WHERE general_version_id = %s", (version_id,))
        if cur.fetchone():
            raise GeneralVersionError(
                "C'est la version générale : choisissez d'abord une autre version générale.")
    extra = ", published_at = NOW()" if new_status == "published" else ""
    scope = " AND plugin_id = %s" if plugin_id is not None else ""
    params = (new_status, version_id) + ((plugin_id,) if plugin_id is not None else ())
    cur.execute(f"""
        UPDATE plugin_versions SET status = %s {extra}
        WHERE id = %s{scope} RETURNING id
    """, params)
    return cur.fetchone() is not None


# ─── Version générale (issue #40) ─────────────────────────────────────
# « Publiée » = disponible pour des campagnes ; « générale » = destinée à tout
# le parc, seule annoncée par les canaux natifs (feed LibreOffice, manifestes
# Chromium/Gecko). Publier une version ne la rend jamais générale d'office.

GENERAL_VERSION_STATUSES = ("published", "deprecated")


def get_general_version(cur, plugin_id: int) -> dict | None:
    """{id, version, status} de la version générale posée, ou None."""
    cur.execute("""
        SELECT pv.id, pv.version, pv.status
        FROM plugins p JOIN plugin_versions pv ON pv.id = p.general_version_id
        WHERE p.id = %s
    """, (plugin_id,))
    row = cur.fetchone()
    return {"id": row[0], "version": row[1], "status": row[2]} if row else None


def set_general_version(cur, plugin_id: int, version_id: int | None) -> tuple:
    """Pose (ou retire, `version_id=None`) la version générale.

    Refuse une version d'un autre plugin, dans un statut non diffusable
    (`draft`, `yanked`, `experimental`) ou sans binaire servable : les canaux
    natifs l'annonceraient à tout le parc pour un téléchargement en échec.
    Renvoie (ancienne version, nouvelle version), en libellés.
    """
    previous = get_general_version(cur, plugin_id)
    new_label = None
    if version_id is not None:
        cur.execute("""
            SELECT pv.version, pv.status,
                   ((pv.distribution_mode = 'managed'
                     AND a.s3_path IS NOT NULL AND a.s3_path <> '')
                    OR (pv.distribution_mode IN ('download_link','store')
                        AND pv.download_url IS NOT NULL AND pv.download_url <> ''))
            FROM plugin_versions pv LEFT JOIN artifacts a ON a.id = pv.artifact_id
            WHERE pv.id = %s AND pv.plugin_id = %s
        """, (version_id, plugin_id))
        row = cur.fetchone()
        if not row:
            raise GeneralVersionError("Version introuvable pour ce plugin.")
        new_label, status, servable = row
        if status not in GENERAL_VERSION_STATUSES:
            raise GeneralVersionError(
                f"Une version « {status} » ne peut pas être la version générale.")
        if not servable:
            raise GeneralVersionError("Cette version n'a pas de binaire téléchargeable.")
    cur.execute("UPDATE plugins SET general_version_id = %s, updated_at = NOW() "
                "WHERE id = %s RETURNING id", (version_id, plugin_id))
    if cur.fetchone() is None:
        raise GeneralVersionError("Plugin introuvable.")
    return (previous["version"] if previous else None), new_label


# ─── Installations ────────────────────────────────────────────────────

def get_plugin_stats(cur, plugin_id: int) -> dict:
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'active') AS active,
            COUNT(*) FILTER (WHERE status = 'inactive') AS inactive,
            COUNT(*) FILTER (WHERE status = 'uninstalled') AS uninstalled,
            COUNT(*) AS total,
            -- Combien de versions distinctes circulent réellement sur le parc.
            -- DOIT venir de l'agrégat : list_installations est paginée (20 lignes
            -- dans la route), un comptage Python sur la page serait faux dès le
            -- 21e appareil.
            COUNT(DISTINCT installed_version) FILTER (
                WHERE status = 'active' AND installed_version IS NOT NULL
            ) AS distinct_versions
        FROM plugin_installations WHERE plugin_id = %s
    """, (plugin_id,))
    row = cur.fetchone()
    return {"active": row[0], "inactive": row[1], "uninstalled": row[2], "total": row[3],
            "distinct_versions": row[4]}


def list_installations(cur, plugin_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    cur.execute("""
        SELECT * FROM plugin_installations
        WHERE plugin_id = %s
        ORDER BY last_seen_at DESC
        LIMIT %s OFFSET %s
    """, (plugin_id, limit, offset))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
