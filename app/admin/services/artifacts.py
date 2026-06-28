"""Artifacts service — CRUD and upload operations."""

from __future__ import annotations

import hashlib
import os

ALLOWED_EXTENSIONS = {".oxt", ".xpi", ".crx"}
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


def list_artifacts(cur) -> list[dict]:
    cur.execute("""
        SELECT a.id, a.device_type, a.platform_variant, a.version,
               a.checksum, a.is_active, a.released_at, a.changelog_url,
               a.s3_path,
               COUNT(c.id) AS campaign_count
        FROM artifacts a
        LEFT JOIN campaigns c ON c.artifact_id = a.id
        GROUP BY a.id
        ORDER BY a.released_at DESC
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_artifact(cur, artifact_id: int) -> dict | None:
    cur.execute("""
        SELECT id, device_type, platform_variant, version, checksum,
               is_active, released_at, changelog_url, s3_path
        FROM artifacts WHERE id = %s
    """, (artifact_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def validate_upload(filename: str, content_length: int) -> str | None:
    """Validate upload. Returns error message or None if valid."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"Extension {ext} non autorisee. Autorisees: {', '.join(ALLOWED_EXTENSIONS)}"
    if content_length > MAX_UPLOAD_SIZE:
        return f"Fichier trop volumineux ({content_length // 1024 // 1024} Mo > 100 Mo)"
    return None


def compute_checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def create_artifact(cur, *, device_type: str, platform_variant: str,
                    version: str, s3_path: str, checksum: str,
                    changelog_url: str = None) -> int:
    cur.execute("""
        INSERT INTO artifacts (device_type, platform_variant, version,
                              s3_path, checksum, changelog_url)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_type, platform_variant, version) DO UPDATE SET
            s3_path = EXCLUDED.s3_path,
            checksum = EXCLUDED.checksum,
            changelog_url = COALESCE(EXCLUDED.changelog_url, artifacts.changelog_url),
            released_at = NOW()
        RETURNING id
    """, (device_type, platform_variant or "", version, s3_path,
          checksum, changelog_url or None))
    return cur.fetchone()[0]


def toggle_artifact(cur, artifact_id: int, is_active: bool) -> bool:
    cur.execute("""
        UPDATE artifacts SET is_active = %s WHERE id = %s RETURNING id
    """, (is_active, artifact_id))
    return cur.fetchone() is not None
