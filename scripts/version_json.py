#!/usr/bin/env python3
"""Écrit le `version.json` que l'image embarque en `/app/version.json` et sert sur `/__version__`.

Convention MirAI next (ADR-0004 de la plateforme), alignée sur la spécification Dockerflow de
Mozilla (https://github.com/mozilla-services/Dockerflow/blob/main/docs/version_object.md) :
champs `source`, `version`, `commit`, `build` ; nos extensions `code_date`, `changes` et `history`.
Le noteur du suivi lit ce fichier DANS le cluster pour rédiger la note de version, sans parler à
GitHub. Bibliothèque standard seulement : ce script tourne dans l'étape de construction.

    python3 scripts/version_json.py --version 0.3.0 --commit <sha> --build <url du run> \
        --code-date <iso> --source https://github.com/<org>/<dépôt> \
        --changelog CHANGELOG.md --out /app/version.json [--history-max 50]

`changes` est la section de CETTE version dans le CHANGELOG tenu par release-please ; vide si
le fichier ou la section manque (premier build, build de poste) — jamais une erreur.
`history` est la liste de TOUTES les sections du CHANGELOG, de la plus récente à la plus
ancienne (`version`, `date`, `changes`), bornée à `--history-max` entrées (50 par défaut) : ce
qui a été fait avant voyage avec l'image, pas seulement ce qui vient de changer.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

TITRE = re.compile(r"^##\s+\[?v?(?P<version>\d[^\]\s(]*)\]?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _nettoyer(lignes: list[str]) -> list[str]:
    while lignes and not lignes[0].strip():
        lignes.pop(0)
    while lignes and not lignes[-1].strip():
        lignes.pop()
    return lignes


def sections(changelog: str) -> list[dict]:
    """Toutes les sections `## [version] (date)`, dans l'ordre du fichier (la plus récente en tête)."""
    resultat: list[dict] = []
    courante: dict | None = None
    for ligne in changelog.splitlines():
        if ligne.startswith("## "):
            if courante is not None:
                courante["changes"] = _nettoyer(courante["changes"])
                resultat.append(courante)
            m = TITRE.match(ligne)
            d = DATE.search(ligne[m.end():]) if m else None
            courante = {"version": m.group("version"), "date": d.group(0) if d else "", "changes": []} if m else None
            continue
        if courante is not None:
            courante["changes"].append(ligne)
    if courante is not None:
        courante["changes"] = _nettoyer(courante["changes"])
        resultat.append(courante)
    return resultat


def section(changelog: str, version: str) -> list[str]:
    """Lignes de la section `## [version]` (ou `## version`), sans le titre, jusqu'à la suivante."""
    for s in sections(changelog):
        if s["version"] == version:
            return s["changes"]
    return []


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--build", default="")
    p.add_argument("--code-date", default="")
    p.add_argument("--source", default="")
    p.add_argument("--changelog", default="CHANGELOG.md")
    p.add_argument("--history-max", type=int, default=50, help="nombre maximal de versions dans `history` (0 = aucune)")
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
        "history": sections(texte)[: max(a.history_max, 0)],
    }
    sortie = pathlib.Path(a.out)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
