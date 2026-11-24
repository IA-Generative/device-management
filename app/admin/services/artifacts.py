"""Artifacts service — CRUD and upload operations."""

from __future__ import annotations

import hashlib
import json
import os

from ...services import package_import

ALLOWED_EXTENSIONS = {".oxt", ".xpi", ".crx"}
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


def list_artifacts(cur) -> list[dict]:
    cur.execute("""
        SELECT a.id, a.device_type, a.platform_variant, a.version,
               a.checksum, a.is_active, a.released_at, a.changelog_url,
               a.s3_path, a.plugin_id, p.slug AS plugin_slug,
               COUNT(c.id) AS campaign_count
        FROM artifacts a
        LEFT JOIN plugins p ON p.id = a.plugin_id
        LEFT JOIN campaigns c ON c.artifact_id = a.id
        GROUP BY a.id, p.slug
        ORDER BY a.released_at DESC
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_artifact(cur, artifact_id: int) -> dict | None:
    cur.execute("""
        SELECT id, device_type, platform_variant, version, checksum,
               is_active, released_at, changelog_url, s3_path, plugin_id
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


class ArtifactTaken(ArtifactConflict):
    """Le numéro de version est déjà pris par un AUTRE plugin du même type (issue #62).

    Aucune confirmation ne lève ce refus : remplacer le binaire servirait aux postes
    de l'autre plugin un paquet qui n'est pas le leur.
    """
    code = "version_taken"


def _bare_checksum(value) -> str:
    text = str(value or "").strip().lower()
    return text.split(":", 1)[1] if ":" in text else text


def _same_number_rows(cur, *, device_type: str, platform_variant: str, version: str) -> list[tuple]:
    """(id, plugin_id, checksum, slug du propriétaire) des artefacts de ce numéro.

    COALESCE : les artefacts créés hors des formulaires ont une variante NULL.
    """
    cur.execute("""
        SELECT a.id, a.plugin_id, a.checksum, p.slug
        FROM artifacts a LEFT JOIN plugins p ON p.id = a.plugin_id
        WHERE a.device_type = %s AND COALESCE(a.platform_variant, '') = %s AND a.version = %s
        ORDER BY a.id
    """, (device_type, platform_variant or "", version))
    return cur.fetchall()


def _split_owner(rows, plugin_id):
    """(artefacts que ce plugin peut remplacer, artefacts d'autres plugins).

    Un artefact appartient à un plugin (artifacts.plugin_id, issue #62). Un artefact
    sans propriétaire (envoyé avant la colonne, ou par une route sans plugin) est
    repris par le premier plugin qui publie ce numéro, comme avant que le plugin
    entre dans la clé. Sans plugin, seuls les artefacts sans propriétaire comptent.
    """
    own, foreign = [], []
    for row in rows:
        owner = row[1]
        if owner is None or (plugin_id is not None and owner == plugin_id):
            own.append(row)
        else:
            foreign.append(row)
    return own, foreign


def check_overwrite(cur, *, device_type: str, platform_variant: str, version: str,
                    checksum: str, confirmed: bool = False, plugin_id: int | None = None) -> bool:
    """Garde du double envoi (issue #132). À appeler AVANT d'écrire le binaire.

    create_artifact remplace l'artefact de ce plugin pour ce numéro (type,
    variante, version). Avec un binaire différent, les postes déjà sur ce numéro
    ne reçoivent rien (égalité de version) : deux binaires coexistent sous le même
    numéro. On refuse donc (ArtifactConflict) sauf confirmation explicite. Même
    binaire : rien à dire. Empreinte existante absente : on laisse passer.

    Numéro déjà pris par un autre plugin du même type : refus sans appel
    (ArtifactTaken), tant que la contrainte d'unicité ne porte pas le plugin.

    Renvoie True si un binaire différent est remplacé (confirmé).
    """
    rows = _same_number_rows(cur, device_type=device_type,
                             platform_variant=platform_variant, version=version)
    own, foreign = _split_owner(rows, plugin_id)
    if foreign and not own:
        owners = ", ".join(sorted({f"« {r[3] or r[1]} »" for r in foreign}))
        raise ArtifactTaken(
            f"La version {version} de type {device_type} est déjà publiée par le plugin "
            f"{owners}. Deux plugins de même type ne peuvent pas encore publier le même "
            "numéro : publiez sous un autre numéro de version.")
    known = [_bare_checksum(row[2]) for row in own if row[2]]
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
                    changelog_url: str = None, bootstrap_info: dict | None = None,
                    plugin_id: int | None = None) -> int:
    """Crée ou remplace l'artefact de ce plugin pour ce numéro ; renvoie son id.

    `bootstrap_info` : relevé de l'import (`package_import.import_package`). Il est
    réparti en trois colonnes : la liste de DM du paquet (issue #133), l'enveloppe
    signée du paquet servi et sa provenance (ADR-0006).

    Aucun `ON CONFLICT (colonnes)` : l'écriture ne nomme pas de contrainte, et
    fonctionne donc avant comme après l'entrée du plugin dans la clé d'unicité.
    """
    bootstrap, signature, provenance = package_import.split_info(bootstrap_info)
    payload = (s3_path, checksum, changelog_url or None,
               json.dumps(bootstrap) if bootstrap else None,
               json.dumps(signature) if signature else None,
               json.dumps(provenance) if provenance else None)

    def _own_row() -> int | None:
        rows = _same_number_rows(cur, device_type=device_type,
                                 platform_variant=platform_variant, version=version)
        own, _foreign = _split_owner(rows, plugin_id)
        # L'artefact déjà à ce plugin d'abord, un artefact sans propriétaire ensuite.
        own.sort(key=lambda r: (r[1] is None, r[0]))
        return own[0][0] if own else None

    def _replace(artifact_id: int) -> int:
        cur.execute("""
            UPDATE artifacts SET
                s3_path = %s,
                checksum = %s,
                changelog_url = COALESCE(%s, changelog_url),
                bootstrap_info = %s,
                signature = %s,
                provenance = %s,
                plugin_id = COALESCE(plugin_id, %s),
                released_at = NOW()
            WHERE id = %s
            RETURNING id
        """, (*payload, plugin_id, artifact_id))
        return cur.fetchone()[0]

    existing = _own_row()
    if existing is not None:
        return _replace(existing)
    cur.execute("""
        INSERT INTO artifacts (device_type, platform_variant, version, plugin_id,
                              s3_path, checksum, changelog_url, bootstrap_info,
                              signature, provenance)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
    """, (device_type, platform_variant or "", version, plugin_id, *payload))
    row = cur.fetchone()
    if row:
        return row[0]
    # Envoi concurrent du même numéro : l'artefact existe désormais.
    existing = _own_row()
    if existing is not None:
        return _replace(existing)
    raise ArtifactTaken(
        f"La version {version} de type {device_type} est déjà publiée par un autre plugin.")


def toggle_artifact(cur, artifact_id: int, is_active: bool) -> bool:
    cur.execute("""
        UPDATE artifacts SET is_active = %s WHERE id = %s RETURNING id
    """, (is_active, artifact_id))
    return cur.fetchone() is not None
