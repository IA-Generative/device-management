"""Protocole `passthrough` du relais d'outils : `{dm}/tools/v1/<service>/<chemin…>` est
propagé vers `{base_url}/<chemin…>`, même verbe, même chaîne de requête, même corps.

Le DM ne connaît pas les objets métier : le corps n'est jamais analysé (un multipart
passe comme le reste). Il n'applique que les bornes du registre : préfixes de chemin
(`paths`, sinon 404), verbes (`methods`, sinon 405), taille du corps (`max_body_mb`,
sinon 413) et délai (`timeout_s`).

En-têtes : tout ce qui identifie le poste ou la personne auprès du DM est retiré, puis le
DM pose ce que l'amont attend : son authentification (`auth.kind` du registre) et, si le
service déclare `agent_header`, l'adresse de la personne tirée de son jeton vérifié. Un
en-tête de ce nom envoyé par le poste ne passe jamais.
"""
from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import quote

import httpx
from fastapi import Request, Response

from . import http_client
from .errors import ToolsError
from .registry import Service

logger = logging.getLogger("device-management.tools")

# En-têtes entrants jamais transmis à l'amont. Le contrat demande : authorization, cookie,
# x-relay-*, x-user-authorization, x-agent-email, host. S'y ajoutent les alias historiques
# des en-têtes relay, ce que posent les mandataires en chemin, et le transport (recalculé
# par le client sortant).
_EN_TETES_RETIRES = frozenset({
    "authorization", "cookie", "x-user-authorization", "x-agent-email", "host",
    "x-client-id", "x-client-key",
    "forwarded", "x-real-ip",
    "content-length", "transfer-encoding", "connection", "keep-alive", "te", "trailer", "upgrade", "expect",
    "accept-encoding",
})
_PREFIXES_RETIRES = ("x-relay-", "x-forwarded-", "proxy-")

_ADRESSE = re.compile(r"^[^\s@<>,;:\"]+@[^\s@<>,;:\"]+$")

# Jetons client_credentials : clé du service → (échéance, jeton). Redemandé un peu avant
# l'expiration annoncée, pour qu'un jeton ne meure pas en route.
_MARGE_EXPIRATION_S = 30
_jetons: dict[str, tuple[float, str]] = {}
_maintenant = time.monotonic


def reset_for_tests() -> None:
    _jetons.clear()


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


def adresse_agent(service: Service, email: str) -> str:
    """Adresse à poser dans `agent_header` ; 403 si le jeton de la personne n'en porte pas
    une exploitable (l'amont résout l'agent par cette adresse : pas d'appel anonyme)."""
    if not service.agent_header:
        return ""
    email = (email or "").strip()
    if len(email) > 254 or not _ADRESSE.match(email):
        raise ToolsError(403, "forbidden", "le jeton de la personne ne porte pas d'adresse e-mail")
    return email


def _secret(service: Service) -> str:
    """Secret du service, lu dans la variable d'environnement nommée par le registre."""
    assert service.auth_amont is not None
    valeur = os.getenv(service.auth_amont.secret_env, "").strip()
    if not valeur:
        logger.error("tools: service %s, variable %s absente ou vide", service.id, service.auth_amont.secret_env)
        raise ToolsError(503, "unavailable", f"{service.label} n'est pas configuré sur ce serveur")
    return valeur


def _cle_jeton(service: Service) -> str:
    assert service.auth_amont is not None
    return f"{service.id}|{service.auth_amont.token_url}|{service.auth_amont.client_id}"


async def _jeton_de_service(service: Service) -> str:
    """Jeton `client_credentials` du service, gardé jusqu'à peu avant son expiration."""
    auth = service.auth_amont
    assert auth is not None
    cle = _cle_jeton(service)
    en_cache = _jetons.get(cle)
    if en_cache and en_cache[0] > _maintenant():
        return en_cache[1]
    indisponible = ToolsError(502, "unavailable", f"authentification auprès de {service.label} indisponible")
    try:
        resp = await http_client.get_async_client().post(
            auth.token_url,
            data={"grant_type": "client_credentials", "client_id": auth.client_id, "client_secret": _secret(service)},
            headers={"Accept": "application/json"}, timeout=http_client.timeout(10.0), follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        logger.warning("tools: %s, serveur de jetons injoignable: %s", service.id, exc.__class__.__name__)
        raise indisponible from exc
    try:
        obj = resp.json() if resp.status_code == 200 else None
    except ValueError:
        obj = None
    jeton = obj.get("access_token") if isinstance(obj, dict) else None
    if not isinstance(jeton, str) or not jeton:
        # Statut seulement : la réponse d'un serveur de jetons peut citer le client ou le secret.
        logger.warning("tools: %s, jeton de service refusé (HTTP %s)", service.id, resp.status_code)
        raise indisponible
    try:
        duree = int(obj.get("expires_in") or 0)
    except (TypeError, ValueError):
        duree = 0
    _jetons[cle] = (_maintenant() + max(0, duree - _MARGE_EXPIRATION_S), jeton)
    return jeton


async def _auth_amont(service: Service) -> tuple[str, str]:
    """(nom, valeur) de l'en-tête d'authentification attendu par l'amont."""
    auth = service.auth_amont
    assert auth is not None
    if auth.kind == "header":
        return auth.header_name.lower(), _secret(service)
    if auth.kind == "static":
        return "authorization", f"Bearer {_secret(service)}"
    return "authorization", f"Bearer {await _jeton_de_service(service)}"


async def en_tetes_sortants(service: Service, request: Request, email: str) -> dict[str, str]:
    """En-têtes de l'appel amont : ceux du poste moins les retirés, plus ceux que pose le DM."""
    headers = {
        k: v for k, v in request.headers.items()
        # Un nom à tiret bas est écarté : certains serveurs le confondent avec le même nom à
        # tirets (`x_agent_email` lu comme `x-agent-email`).
        if k not in _EN_TETES_RETIRES and not k.startswith(_PREFIXES_RETIRES) and "_" not in k
    }
    # Ce que le DM pose écrase ce que le poste aurait envoyé sous le même nom.
    nom, valeur = await _auth_amont(service)
    headers[nom] = valeur
    if service.agent_header:
        headers[service.agent_header.lower()] = email
    headers["accept-encoding"] = "identity"
    return headers


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


async def relayer(service: Service, request: Request, chemin: str, corps: bytes, email: str) -> Response:
    """Appelle l'amont et rend sa réponse."""
    url = service.upstream + chemin
    if request.url.query:
        url += "?" + request.url.query
    sortante = httpx.Request(
        request.method, url, headers=await en_tetes_sortants(service, request, email), content=corps,
        extensions={"timeout": http_client.timeout(float(service.timeout_s)).as_dict()},
    )
    resp = await http_client.get_async_client().send(sortante, follow_redirects=False)
    if resp.status_code == 401 and service.auth_amont and service.auth_amont.kind == "client_credentials":
        _jetons.pop(_cle_jeton(service), None)  # jeton refusé : le prochain appel en redemande un
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))
