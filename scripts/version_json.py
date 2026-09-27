#!/usr/bin/env python3
"""Écrit le `version.json` que l'image embarque en `/app/version.json` et sert sur `/__version__`.

Convention MirAI next (ADR-0004 de la plateforme), alignée sur la spécification Dockerflow de
Mozilla (https://github.com/mozilla-services/Dockerflow/blob/main/docs/version_object.md) :
champs `source`, `version`, `commit`, `build` ; nos extensions `code_date` et `changes`. Le
noteur du suivi lit ce fichier DANS le cluster pour rédiger la note de version, sans parler à
GitHub. Bibliothèque standard seulement : ce script tourne dans l'étape de construction.

    python3 scripts/version_json.py --version 0.3.0 --commit <sha> --build <url du run> \
        --code-date <iso> --source https://github.com/<org>/<dépôt> \
        --changelog CHANGELOG.md --out /app/version.json

`changes` est la section de CETTE version dans le CHANGELOG tenu par release-please ; vide si
le fichier ou la section manque (premier build, build de poste) — jamais une erreur.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re


def section(changelog: str, version: str) -> list[str]:
    """Lignes de la section `## [version]` (ou `## version`), sans le titre, jusqu'à la suivante."""
    lignes, dedans = [], False
    titre = re.compile(r"^##\s+\[?v?" + re.escape(version) + r"\]?(\s|\(|$)")
    for ligne in changelog.splitlines():
        if ligne.startswith("## "):
            if dedans:
                break
            dedans = bool(titre.match(ligne))
            continue
        if dedans:
            lignes.append(ligne)
    while lignes and not lignes[0].strip():
        lignes.pop(0)
    while lignes and not lignes[-1].strip():
        lignes.pop()
    return lignes


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--build", default="")
    p.add_argument("--code-date", default="")
    p.add_argument("--source", default="")
    p.add_argument("--changelog", default="CHANGELOG.md")
    p.add_argument("--out", required=True)
    a = p.parse_args()

    chemin = pathlib.Path(a.changelog)
    texte = chemin.read_text(encoding="utf-8") if chemin.is_file() else ""
    contenu = {
        # Dockerflow
        "source": a.source,
        "version": a.version,
        "commit": a.commit,
        "build": a.build,
        # Extensions MirAI next
        "code_date": a.code_date,
        "changes": section(texte, a.version),
    }
    sortie = pathlib.Path(a.out)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
