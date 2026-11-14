"""Artifacts service — CRUD and upload operations."""

from __future__ import annotations

import hashlib
import json
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
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


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
    return dict(zip(cols, row, strict=False))


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


class ArtifactConflict(ValueError):
    """Le numéro de version existe déjà avec un autre binaire, sans confirmation."""


def _bare_checksum(value) -> str:
    text = str(value or "").strip().lower()
    return text.split(":", 1)[1] if ":" in text else text


def check_overwrite(cur, *, device_type: str, platform_variant: str, version: str,
                    checksum: str, confirmed: bool = False) -> bool:
    """Garde du double envoi (issue #132). À appeler AVANT d'écrire le binaire.

    create_artifact écrase l'artefact de même (type, variante, version). Avec un
    binaire différent, les postes déjà sur ce numéro ne reçoivent rien (égalité
    de version) : deux binaires coexistent sous le même numéro. On refuse donc
    (ArtifactConflict) sauf confirmation explicite. Même binaire : rien à dire.
    Empreinte existante absente : pas de comparaison possible, on laisse passer.

    Renvoie True si un binaire différent est remplacé (confirmé).
    """
    # COALESCE : les artefacts créés hors des formulaires ont une variante NULL.
    cur.execute("""
        SELECT checksum FROM artifacts
        WHERE device_type = %s AND COALESCE(platform_variant, '') = %s AND version = %s
    """, (device_type, platform_variant or "", version))
    known = [_bare_checksum(row[0]) for row in cur.fetchall() if row[0]]
    if not known or _bare_checksum(checksum) in known:
        return False
    if not confirmed:
        raise ArtifactConflict(
            f"La version {version} existe déjà avec un binaire différent. Les postes déjà en "
            f"{version} ne recevront pas le nouveau binaire. Publiez sous un nouveau numéro de "
            "version, ou confirmez le remplacement.")
    return True


def create_artifact(cur, *, device_type: str, platform_variant: str,
                    version: str, s3_path: str, checksum: str,
                    changelog_url: str = None, bootstrap_info: dict | None = None) -> int:
    """`bootstrap_info` : relevé de la liste de DM du paquet (issue #133)."""
    cur.execute("""
        INSERT INTO artifacts (device_type, platform_variant, version,
                              s3_path, checksum, changelog_url, bootstrap_info)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_type, platform_variant, version) DO UPDATE SET
            s3_path = EXCLUDED.s3_path,
            checksum = EXCLUDED.checksum,
            changelog_url = COALESCE(EXCLUDED.changelog_url, artifacts.changelog_url),
            bootstrap_info = EXCLUDED.bootstrap_info,
            released_at = NOW()
        RETURNING id
    """, (device_type, platform_variant or "", version, s3_path,
          checksum, changelog_url or None,
          json.dumps(bootstrap_info) if bootstrap_info else None))
    return cur.fetchone()[0]


def toggle_artifact(cur, artifact_id: int, is_active: bool) -> bool:
    cur.execute("""
        UPDATE artifacts SET is_active = %s WHERE id = %s RETURNING id
    """, (is_active, artifact_id))
    return cur.fetchone() is not None
