"""Complément Word (IAssistant-Word) servi par le DM : `GET /addins/word/{fichier}`.

Le complément est une page web qu'Office charge depuis l'adresse écrite dans son manifeste.
Le DM la sert sur sa propre origine, si bien que ses appels à `/config`, `/enroll`,
`/auth/token` et `/tools/v1` restent de même origine, sans CORS.

Les fichiers sont lus tels quels dans le répertoire `DM_ADDIN_WORD_DIR` (le dossier `addin/`
du dépôt IAssistant-Word, monté dans le pod). Une nouvelle version du complément est un nouveau
contenu de ce répertoire. Sans répertoire configuré, la route répond 404.

`manifest.xml` est rendu à la volée : `{{BASE}}` devient l'adresse publique des fichiers,
`{{PARAMS}}` le profil demandé (`?profil=int`, vide sans profil), `{{KEYCLOAK}}` l'origine du SSO
et `{{VERSION}}` la version du complément.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from . import runtime_config
from .pathsafe import safe_path_join

router = APIRouter()

PREFIX = "/addins/word/"
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    # Un module ES servi sous un autre type n'est pas exécuté par le navigateur de Word.
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".xml": "application/xml; charset=utf-8",
}
_PROFIL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Office.js vient du CDN de Microsoft ; tout le reste est servi par le DM. D'abord en
# « Report-Only » : Office.js charge ses propres ressources, à observer avant d'imposer.
CSP_REPORT_ONLY = (
    "default-src 'self'; script-src 'self' https://appsforoffice.microsoft.com; "
    "style-src 'self'; img-src 'self' data:; connect-src 'self'"
)
# Word garde longtemps les fichiers d'un complément : on lui demande de revalider.
_HEADERS = {"Cache-Control": "no-cache", "Content-Security-Policy-Report-Only": CSP_REPORT_ONLY}


def _addin_dir() -> str:
    directory = str(runtime_config.cfg("DM_ADDIN_WORD_DIR", "") or "").strip()
    if not directory or not os.path.isdir(directory):
        raise HTTPException(status_code=404, detail="Complément Word non publié sur ce DM.")
    return directory


def _keycloak_origin() -> str:
    # Le SSO dont le DM vérifie les jetons (mêmes réglages que DM_AUTH_ISSUER_URL) : seule l'origine compte.
    for key in ("DM_AUTH_ISSUER_URL", "AUTH_ISSUER_URL", "KEYCLOAK_ISSUER_URL"):
        parts = urlsplit(str(runtime_config.cfg(key, "") or "").strip())
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def render_manifest(template: str, profil: str | None) -> str:
    base = str(runtime_config.cfg("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    version = str(runtime_config.cfg("DM_ADDIN_WORD_VERSION", "") or "").strip() or "0.1.0.0"
    return (
        template.replace("{{BASE}}", f"{base}/addins/word")
        .replace("{{PARAMS}}", f"?profil={quote(profil)}" if profil else "")
        .replace("{{KEYCLOAK}}", _keycloak_origin())
        .replace("{{VERSION}}", version)
    )


@router.get("/addins/word/manifest.xml", include_in_schema=False)
def word_manifest(profil: str | None = Query(default=None)) -> Response:
    if profil is not None and not _PROFIL.fullmatch(profil):
        raise HTTPException(status_code=400, detail="Profil invalide.")
    path = safe_path_join(_addin_dir(), "manifest.xml")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    with open(path, encoding="utf-8") as handle:
        content = render_manifest(handle.read(), profil)
    return Response(content, media_type=_TYPES[".xml"], headers={"Cache-Control": "no-cache"})


@router.get("/addins/word/{fichier:path}", include_in_schema=False)
def word_file(fichier: str) -> FileResponse:
    path = safe_path_join(_addin_dir(), fichier)
    media_type = _TYPES.get(os.path.splitext(path)[1].lower())
    if media_type is None or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    return FileResponse(path, media_type=media_type, headers=_HEADERS)
