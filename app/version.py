"""Convention ADR-0004 MirAI next, format Dockerflow : `/app/version.json` servi sur `/__version__`.

L'image écrit ce fichier au moment où elle se construit (voir `deploy/docker/Dockerfile` et
`scripts/version_json.py`) : source, version, commit, build, date du code et changements de cette
version. Le noteur de la plateforme lit cette route par le Service interne, sans jamais parler à
GitHub, pour rédiger la note de version des testeurs.

Hors image (tests, `uvicorn` sur un poste), le fichier manque : on répond « dev », jamais une
erreur — une version inconnue n'est pas une panne.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

VERSION_JSON = Path("/app/version.json")
DEV = {"source": "", "version": "dev", "commit": "", "build": "", "code_date": "", "changes": []}

router = APIRouter()


@router.get("/__version__", include_in_schema=False)
def version_json() -> dict:
    """Le journal que l'image porte, tel quel."""
    try:
        return json.loads(VERSION_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEV)
