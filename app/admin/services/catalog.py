"""Catalog service — plugins, versions, bundles, installations."""

from __future__ import annotations
import json


# ─── Plugins ──────────────────────────────────────────────────────────

def list_plugins(cur, *, status: str = None, device_type: str = None,
                 category: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
    conditions, params = [], []
    if status:
        conditions.append("p.status = %s"); params.append(status)
    if device_type:
        conditions.append("p.device_type = %s"); params.append(device_type)
    if category:
        conditions.append("p.category = %s"); params.append(category)
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
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_plugin(cur, plugin_id: int) -> dict | None:
    cur.execute("SELECT * FROM plugins WHERE id = %s", (plugin_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_plugin_by_slug(cur, slug: str) -> dict | None:
    cur.execute("SELECT * FROM plugins WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def create_plugin(cur, *, slug: str, name: str, description: str = "",
                  intent: str = "", key_features: list = None,
                  changelog: str = "", device_type: str = "libreoffice",
                  category: str = "productivity", icon_url: str = "",
                  homepage_url: str = "", support_email: str = "",
                  publisher: str = "DNUM", visibility: str = "public") -> int:
    cur.execute("""
        INSERT INTO plugins (slug, name, description, intent, key_features, changelog,
                            device_type, category, icon_url, homepage_url, support_email,
                            publisher, visibility, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
        RETURNING id
    """, (slug, name, description, intent,
          json.dumps(key_features or []), changelog,
          device_type, category, icon_url, homepage_url, support_email,
          publisher, visibility))
    return cur.fetchone()[0]


def update_plugin(cur, plugin_id: int, **fields) -> bool:
    allowed = {"name", "description", "intent", "key_features", "changelog",
               "category", "icon_url", "homepage_url", "support_email",
               "publisher", "visibility", "status"}
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
    return [dict(zip(cols, row)) for row in cur.fetchall()]


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
    return dict(zip(cols, row))


def create_version(cur, *, plugin_id: int, version: str, artifact_id: int = None,
                   release_notes: str = "", download_url: str = "",
                   min_host_version: str = "", max_host_version: str = "",
                   distribution_mode: str = "managed",
                   status: str = "draft") -> int:
    cur.execute("""
        INSERT INTO plugin_versions
            (plugin_id, version, artifact_id, release_notes, download_url,
             min_host_version, max_host_version, distribution_mode, status,
             published_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s = 'published' THEN NOW() ELSE NULL END)
        RETURNING id
    """, (plugin_id, version, artifact_id or None, release_notes,
          download_url or None, min_host_version or None,
          max_host_version or None, distribution_mode, status, status))
    return cur.fetchone()[0]


def update_version_status(cur, version_id: int, new_status: str) -> bool:
    extra = ", published_at = NOW()" if new_status == "published" else ""
    cur.execute(f"""
        UPDATE plugin_versions SET status = %s {extra}
        WHERE id = %s RETURNING id
    """, (new_status, version_id))
    return cur.fetchone() is not None


# ─── Bundles ──────────────────────────────────────────────────────────

def list_bundles(cur) -> list[dict]:
    cur.execute("""
        SELECT b.*, COUNT(bp.plugin_id) AS plugin_count
        FROM bundles b
        LEFT JOIN bundle_plugins bp ON bp.bundle_id = b.id
        GROUP BY b.id
        ORDER BY b.name
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_bundle(cur, bundle_id: int) -> dict | None:
    cur.execute("SELECT * FROM bundles WHERE id = %s", (bundle_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_bundle_plugins(cur, bundle_id: int) -> list[dict]:
    cur.execute("""
        SELECT p.id, p.slug, p.name, p.device_type, p.icon_url, p.intent,
               bp.is_required, bp.display_order
        FROM bundle_plugins bp
        JOIN plugins p ON p.id = bp.plugin_id
        WHERE bp.bundle_id = %s
        ORDER BY bp.display_order, p.name
    """, (bundle_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_bundle(cur, *, slug: str, name: str, description: str = "",
                  icon_url: str = "", visibility: str = "public") -> int:
    cur.execute("""
        INSERT INTO bundles (slug, name, description, icon_url, visibility)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (slug, name, description, icon_url, visibility))
    return cur.fetchone()[0]


def add_bundle_plugin(cur, bundle_id: int, plugin_id: int,
                      is_required: bool = True, display_order: int = 0):
    cur.execute("""
        INSERT INTO bundle_plugins (bundle_id, plugin_id, is_required, display_order)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (bundle_id, plugin_id) DO UPDATE SET is_required = %s, display_order = %s
    """, (bundle_id, plugin_id, is_required, display_order, is_required, display_order))


def remove_bundle_plugin(cur, bundle_id: int, plugin_id: int):
    cur.execute("DELETE FROM bundle_plugins WHERE bundle_id = %s AND plugin_id = %s",
                (bundle_id, plugin_id))


# ─── Installations ────────────────────────────────────────────────────

def get_plugin_stats(cur, plugin_id: int) -> dict:
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'active') AS active,
            COUNT(*) FILTER (WHERE status = 'inactive') AS inactive,
            COUNT(*) FILTER (WHERE status = 'uninstalled') AS uninstalled,
            COUNT(*) AS total
        FROM plugin_installations WHERE plugin_id = %s
    """, (plugin_id,))
    row = cur.fetchone()
    return {"active": row[0], "inactive": row[1], "uninstalled": row[2], "total": row[3]}


def list_installations(cur, plugin_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    cur.execute("""
        SELECT * FROM plugin_installations
        WHERE plugin_id = %s
        ORDER BY last_seen_at DESC
        LIMIT %s OFFSET %s
    """, (plugin_id, limit, offset))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
