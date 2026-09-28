#!/usr/bin/env python3
"""Le CHANGELOG peut-il être servi à tout le monde ? — contrôle lexical avant qu'il entre dans l'image.

Le CHANGELOG est servi tel quel sur /__version__ (champs `changes` et `history`), sans
authentification : il ne doit rien porter de sensible, et il doit rester lisible par quelqu'un
qui n'est ni technicien ni membre du projet (voir references/changelog.md).

    python3 scripts/verif-changelog.py CHANGELOG.md [--strict]

Sortie : une ligne par constat (`fichier:ligne: ÉCHEC|AVERTISSEMENT — motif`), puis un bilan.
Code 1 si un ÉCHEC (motif sensible, structure cassée) ; avec --strict, aussi si un AVERTISSEMENT
(détail technique). Bibliothèque standard seulement. Le contrôle attrape ce qui se voit, pas ce
qui se comprend : il ne remplace pas la relecture.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ── Sensible : ÉCHEC ──────────────────────────────────────────────────────────
SENSIBLE: list[tuple[str, re.Pattern[str]]] = [
    ("adresse IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("hôte interne", re.compile(r"\b[\w.-]+\.(?:svc\.cluster\.local|svc|internal|local|lan|corp|intra)\b", re.I)),
    ("registre d'images", re.compile(r"\b(?:[\w-]+\.)*(?:azurecr\.io|scw\.cloud|amazonaws\.com|gcr\.io|pkg\.dev)\b", re.I)),
    ("courriel", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("chemin de poste", re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)")),
    ("jeton ou clé", re.compile(r"\b(?:ghp_|gho_|github_pat_|xox[bap]-|AKIA|sk-[A-Za-z0-9]{8,}|eyJ[A-Za-z0-9_-]{16,})")),
    ("secret en clair", re.compile(r"\b(?:mot de passe|password|passwd|secret|token|jeton|api[_ -]?key|cl[ée] (?:api|priv[ée]e))\s*[:=]\s*\S+", re.I)),
    ("condensé ou clé longue", re.compile(r"\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/]{48,}={0,2}\b")),
]

# ── Détail technique : AVERTISSEMENT ─────────────────────────────────────────
TECHNIQUE: list[tuple[str, re.Pattern[str]]] = [
    ("code en apostrophes inversées", re.compile(r"`[^`]+`")),
    ("nom de fichier", re.compile(r"\b[\w./-]+\.(?:py|yaml|yml|json|sh|ts|tsx|js|sql|toml|ini|conf|xml|html|md)\b")),
    ("variable d'environnement", re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:_[A-Z0-9]+){1,}\b")),
    ("identifiant de code", re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b|\b[a-z]+[A-Z][A-Za-z0-9]+\b")),
    ("code de lint ou d'outil", re.compile(r"\b(?:[A-Z]{1,3}\d{3,4}|KSV\d+|DS\d+|CVE-\d{4}-\d+)\b")),
    ("code HTTP", re.compile(r"\b(?:20[0-9]|30[0-9]|4[0-5][0-9]|50[0-9])\b(?!\s*(?:postes|appareils|utilisateurs|%))")),
    ("chemin d'URL ou de route", re.compile(r"(?<![\w:])/(?:[\w{}.-]+/)*[\w{}.-]+(?:\?[\w=&]+)?(?=[\s,;.)]|$)")),
    ("jargon", re.compile(
        r"\b(?:refactor\w*|fixture|monkeypatch|traceback|stack ?trace|NameError|TypeError|pool|cursor|"
        r"commit|merge|rebase|lint\w*|nosec|noqa|SAST|FK|ON DELETE|CASCADE|SQL|psycopg\w*|uvicorn|"
        r"kubelet|runAs\w+|PVC|StatefulSet|Deployment|namespace|ingress|Helm|Kaniko|BuildKit|Dockerfile|"
        r"uid|jti|cuid|OTLP|OIDC|JWT|Fernet|deep-merge|passthrough|endpoint|middleware|router|"
        r"heartbeat|reaper|worker|queue|flake|smoke)\b", re.I)),
]

TITRE = re.compile(r"^##\s+\[?v?(?P<version>\d+\.\d+\.\d+[^\]\s(]*)\]?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
LONGUEUR_MAX = 240


def controler(chemin: Path) -> tuple[int, int]:
    echecs = avertissements = 0

    def constat(no: int, niveau: str, motif: str, extrait: str) -> None:
        nonlocal echecs, avertissements
        if niveau == "ÉCHEC":
            echecs += 1
        else:
            avertissements += 1
        print(f"{chemin}:{no}: {niveau} — {motif} : « {extrait.strip()[:90]} »")

    lignes = chemin.read_text(encoding="utf-8").splitlines()
    section_courante: int | None = None
    section_pleine = False
    sections = 0
    for no, ligne in enumerate(lignes, 1):
        if ligne.startswith("## "):
            if section_courante is not None and not section_pleine:
                constat(section_courante, "ÉCHEC", "section vide", lignes[section_courante - 1])
            section_courante, section_pleine = no, False
            sections += 1
            m = TITRE.match(ligne)
            if not m:
                constat(no, "ÉCHEC", "titre sans version X.Y.Z (le script de version ne le lira pas)", ligne)
            elif not DATE.search(ligne[m.end():]):
                constat(no, "AVERTISSEMENT", "titre sans date", ligne)
            continue
        if not ligne.strip() or ligne.startswith("#"):
            continue
        if section_courante is not None and not ligne.startswith("### "):
            section_pleine = True
        # Le préambule (avant la première section) est soumis aux mêmes règles.
        for motif, rx in SENSIBLE:
            m = rx.search(ligne)
            if m:
                constat(no, "ÉCHEC", motif, m.group(0))
        if ligne.startswith(("* ", "- ")) or section_courante is not None:
            for motif, rx in TECHNIQUE:
                m = rx.search(ligne)
                if m:
                    constat(no, "AVERTISSEMENT", motif, m.group(0))
            if len(ligne) > LONGUEUR_MAX:
                constat(no, "AVERTISSEMENT", f"ligne de plus de {LONGUEUR_MAX} caractères", ligne)
    if section_courante is not None and not section_pleine:
        constat(section_courante, "ÉCHEC", "section vide", lignes[section_courante - 1])
    if sections == 0:
        constat(1, "ÉCHEC", "aucune section « ## [X.Y.Z] »", lignes[0] if lignes else "")
    return echecs, avertissements


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("fichiers", nargs="+")
    p.add_argument("--strict", action="store_true", help="un avertissement suffit à échouer")
    a = p.parse_args()

    total_e = total_a = 0
    for f in a.fichiers:
        chemin = Path(f)
        if not chemin.is_file():
            print(f"{chemin}: ÉCHEC — fichier absent")
            total_e += 1
            continue
        e, av = controler(chemin)
        total_e += e
        total_a += av
    verdict = "ÉCHEC" if total_e or (a.strict and total_a) else "OK"
    print(f"{verdict} : {total_e} motif(s) sensible(s) ou de structure, {total_a} détail(s) technique(s)")
    return 1 if verdict == "ÉCHEC" else 0


if __name__ == "__main__":
    sys.exit(main())
