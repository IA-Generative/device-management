"""Outillage des tests de la chaîne de confiance des paquets (ADR-0006).

Clés jetables tirées à chaque session : aucune clé n'est jamais écrite sur disque.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.services import package_signing as ps

_DATE = (2026, 1, 1, 0, 0, 0)


def new_key():
    return ec.generate_private_key(ec.SECP256R1())


def public_pem(key) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def private_pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def delegation(root, dm_key, *, days_before: int = 1, days_after: int = 30, dm: str = "https://dm.test") -> dict:
    now = datetime.datetime.now(datetime.UTC)
    return ps.make_delegation(root, dm_key.public_key(), dm=dm,
                              not_before=now - datetime.timedelta(days=days_before),
                              not_after=now + datetime.timedelta(days=days_after))


def xpi(version: str = "", *, urls: str = "https://dm.example.invalid", extension_id: str = "plugin@example.invalid",
        config: dict | None = None, extra: dict | None = None) -> bytes:
    """Paquet Thunderbird minimal, neutre par défaut (adresse d'exemple)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        rdf = f"<RDF><Description><em:id>{extension_id}</em:id>"
        if version:
            rdf += f"<em:version>{version}</em:version>"
        rdf += ("<em:targetApplication><Description><em:id>{3550f703-e582-4d05-9a08-453d09bdfdc6}</em:id>"
                "</Description></em:targetApplication></Description></RDF>")
        zf.writestr(zipfile.ZipInfo("install.rdf", _DATE), rdf)
        zf.writestr(zipfile.ZipInfo("defaults/preferences/prefs.js", _DATE),
                    f"pref('extensions.IAssistant.bootstrapUrls', '{urls}');\n")
        if config is not None:
            zf.writestr(zipfile.ZipInfo("dm-config.json", _DATE), json.dumps(config))
        for name, content in (extra or {}).items():
            zf.writestr(zipfile.ZipInfo(name, _DATE), content)
    return buf.getvalue()


def build_envelope(key, data: bytes, *, slug: str = "plugin", version: str = "1.0.0",
                   ref: str = "refs/heads/main", repository: str = "org/depot", kind: str = "build") -> dict:
    statement = {
        "schema": ps.STATEMENT_SCHEMA, "kind": kind, "slug": slug, "version": version,
        "file": f"{slug}-{version}.xpi", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "source": {"repository": repository, "ref": ref, "commit": "a" * 40, "run_id": "7"},
        "built_at": "2026-11-15T00:00:00Z",
    }
    return ps.make_envelope(statement, key)


def statement_of(envelope: dict) -> dict:
    import base64
    return json.loads(base64.b64decode(envelope["payload"]))
