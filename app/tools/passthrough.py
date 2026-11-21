"""Protocole `passthrough` du relais d'outils : `{dm}/tools/v1/<service>/<chemin…>` est
propagé vers `{base_url}/<chemin…>`, même verbe, même chaîne de requête, même corps.

Le DM ne connaît pas les objets métier : le corps n'est jamais analysé (un multipart
passe comme le reste). Il n'applique que les bornes du registre : préfixes de chemin
(`paths`, sinon 404), verbes (`methods`, sinon 405), taille du corps (`max_body_mb`,
sinon 413) et délai (`timeout_s`).
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from fastapi import Request, Response

from . import http_client
from .errors import ToolsError
from .registry import Service

logger = logging.getLogger("device-management.tools")


def reset_for_tests() -> None:
    pass


def verifier_methode(service: Service, methode: str) -> None:
    if methode not in service.methods:
        raise ToolsError(405, "method_not_allowed", f"{methode} n'est pas autorisé sur {service.label}")


def chemin_amont(service: Service, chemin: str) -> str:
    """Chemin à appeler chez l'amont, ou 404 s'il sort des préfixes autorisés.

    `chemin` arrive déjà décodé une fois par le serveur. Tout ce qui pourrait faire sortir
    du préfixe après coup est refusé : segments `.` et `..`, barre oblique inversée,
    caractère de contrôle, et `%` restant (double encodage).
    """
    chemin = "/" + chemin
    segments = chemin.split("/")
    suspect = (
        "." in segments or ".." in segments or "\\" in chemin or "%" in chemin
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in chemin)
    )
    if suspect or not any(_sous_prefixe(chemin, p) for p in service.paths):
        raise ToolsError(404, "not_found", "chemin inconnu ou non autorisé sur ce service")
    return quote(chemin, safe="/-._~!$&'()*+,;=:@")


def _sous_prefixe(chemin: str, prefixe: str) -> bool:
    """Un préfixe s'arrête à une frontière de segment : `/v1/` n'ouvre pas `/v1beta`."""
    if prefixe.endswith("/"):
        return chemin.startswith(prefixe)
    return chemin == prefixe or chemin.startswith(prefixe + "/")


async def lire_corps(request: Request, limite: int) -> bytes:
    """Corps de la requête, lu sans analyse ; 413 dès que la borne du service est dépassée."""
    trop_gros = ToolsError(413, "invalid_arguments", "corps trop volumineux pour ce service")
    annonce = request.headers.get("content-length", "")
    if annonce.isdigit() and int(annonce) > limite:
        raise trop_gros
    morceaux: list[bytes] = []
    lu = 0
    async for morceau in request.stream():
        lu += len(morceau)
        if lu > limite:
            raise trop_gros
        morceaux.append(morceau)
    return b"".join(morceaux)


async def relayer(service: Service, request: Request, chemin: str, corps: bytes) -> Response:
    """Appelle l'amont et rend sa réponse."""
    url = service.upstream + chemin
    if request.url.query:
        url += "?" + request.url.query
    headers = {}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    sortante = httpx.Request(
        request.method, url, headers=headers, content=corps,
        extensions={"timeout": http_client.timeout(float(service.timeout_s)).as_dict()},
    )
    resp = await http_client.get_async_client().send(sortante, follow_redirects=False)
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))
