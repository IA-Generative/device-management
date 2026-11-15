#!/usr/bin/env python3
"""Clés et signatures de la chaîne de confiance des paquets (ADR-0006).

Outil d'exploitation, à lancer depuis la racine du dépôt (il réutilise
app/services/package_signing.py, donc le format exact que vérifie le DM).

    # 1. Racine de l'organisation — une fois, sur un poste hors ligne.
    python scripts/package-signing/dm-package-keys.py new-key racine

    # 2. Clé d'un DM, puis sa délégation signée par la racine (12 mois).
    python scripts/package-signing/dm-package-keys.py new-key dm-prod
    python scripts/package-signing/dm-package-keys.py delegate \\
        --root racine.key.pem --dm-public dm-prod.pub.pem \\
        --dm https://dm.exemple.fr --days 365 > dm-prod.delegation.json

    # 3. Clé de la chaîne de build d'un plugin (secret d'environnement GitHub).
    python scripts/package-signing/dm-package-keys.py new-key build-plugin

    # Signer un paquet comme le ferait la CI, puis vérifier une enveloppe.
    python scripts/package-signing/dm-package-keys.py sign-build paquet.xpi \\
        --key build-plugin.key.pem --slug mon-plugin --version 1.2.0 \\
        --repository org/depot --ref refs/heads/main > paquet.xpi.sig.json
    python scripts/package-signing/dm-package-keys.py verify paquet.xpi paquet.xpi.sig.json \\
        --public build-plugin.pub.pem

Où vont les fichiers :
  racine.pub.pem          → DM_PACKAGE_TRUST_ROOTS (values du chart, tous les DM)
  racine.key.pem          → coffre hors ligne ; ne sert qu'à signer des délégations
  dm-prod.key.pem         → DM_PACKAGE_SIGNING_KEY (Secret du composant d'administration)
  dm-prod.delegation.json → DM_PACKAGE_SIGNING_DELEGATION (même Secret)
  build-plugin.pub.pem    → DM_PACKAGE_PUBLISHER_KEYS (values du chart)
  build-plugin.key.pem    → secret PACKAGE_SIGNING_KEY de l'environnement GitHub du plugin

Une clé privée n'est jamais affichée : elle est écrite dans un fichier en 0600.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from app.services import package_signing as ps  # noqa: E402


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_private(path: str, content: bytes) -> None:
    if os.path.exists(path):
        sys.exit(f"{path} existe déjà : refus de l'écraser.")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)


def cmd_new_key(args) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    _write_private(f"{args.name}.key.pem", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with open(f"{args.name}.pub.pem", "wb") as fh:
        fh.write(public)
    print(f"{args.name}.key.pem (privée, 0600) et {args.name}.pub.pem écrits.", file=sys.stderr)
    print(f"keyid : {ps.key_id(key.public_key())}", file=sys.stderr)


def cmd_delegate(args) -> None:
    root = ps.load_private_key(_read(args.root))
    dm_public = serialization.load_pem_public_key(_read(args.dm_public))
    now = datetime.datetime.now(datetime.UTC)
    delegation = ps.make_delegation(root, dm_public, dm=args.dm, not_before=now - datetime.timedelta(minutes=5),
                                    not_after=now + datetime.timedelta(days=args.days))
    print(json.dumps(delegation))
    print(f"Délégation pour la clé {ps.key_id(dm_public)}, valable {args.days} jours.", file=sys.stderr)


def cmd_sign_build(args) -> None:
    data = _read(args.package)
    statement = {
        "schema": ps.STATEMENT_SCHEMA, "kind": "build", "slug": args.slug, "version": args.version,
        "file": os.path.basename(args.package), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "source": {"repository": args.repository, "ref": args.ref, "commit": args.commit,
                   "workflow": args.workflow, "run_id": args.run_id},
        "built_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(json.dumps(ps.make_envelope(statement, ps.load_private_key(_read(args.key)))))


def cmd_verify(args) -> None:
    keys = ps.load_public_keys(_read(args.public))
    try:
        statement, signer = ps.verify_envelope(_read(args.envelope), keys)
    except ps.SignatureError as e:
        sys.exit(f"REFUSÉ ({e.code}) : {e}")
    digest = hashlib.sha256(_read(args.package)).hexdigest()
    if statement.get("sha256") != digest:
        sys.exit(f"REFUSÉ (digest_mismatch) : signé {statement.get('sha256')}, fichier {digest}")
    print(f"OK — signé par {signer} : {statement.get('kind')} {statement.get('slug')} {statement.get('version')}")
    for key in ("source", "dm", "upstream", "changed_files", "extension_id"):
        if statement.get(key):
            print(f"  {key} : {json.dumps(statement[key], ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new-key", help="paire de clés P-256 : <nom>.key.pem et <nom>.pub.pem")
    p.add_argument("name")
    p.set_defaults(func=cmd_new_key)

    p = sub.add_parser("delegate", help="délégation de signature d'un DM, signée par la racine")
    p.add_argument("--root", required=True, help="clé privée de la racine (PEM)")
    p.add_argument("--dm-public", required=True, help="clé publique du DM (PEM)")
    p.add_argument("--dm", required=True, help="adresse publique du DM")
    p.add_argument("--days", type=int, default=365)
    p.set_defaults(func=cmd_delegate)

    p = sub.add_parser("sign-build", help="enveloppe de build d'un paquet (ce que fait la CI du plugin)")
    p.add_argument("package")
    p.add_argument("--key", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--repository", default="")
    p.add_argument("--ref", default="")
    p.add_argument("--commit", default="")
    p.add_argument("--workflow", default="")
    p.add_argument("--run-id", default="")
    p.set_defaults(func=cmd_sign_build)

    p = sub.add_parser("verify", help="vérifie une enveloppe contre un paquet et des clés publiques")
    p.add_argument("package")
    p.add_argument("envelope")
    p.add_argument("--public", required=True, help="clés publiques acceptées (PEM, une ou plusieurs)")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
