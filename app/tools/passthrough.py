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

Option `follow` du service : sur les chemins qu'elle liste, l'amont ne rend pas le fichier
mais son adresse (téléchargement présigné). Le DM va alors chercher le fichier lui-même
et rend son contenu : le poste ne connaît aucune adresse en dehors du DM.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from urllib.parse import quote

import anyio
import httpx
from fastapi import Request, Response

from . import http_client
from .errors import ToolsError
from .registry import Service, Suivi

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

# En-têtes de la réponse amont rendus au poste ; aucun autre ne sort (ni Set-Cookie, ni Server).
_EN_TETES_REPONSE = ("content-type", "location", "etag", "retry-after")

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
    except httpx.TimeoutException as exc:
        raise ToolsError(504, "unavailable", f"authentification auprès de {service.label} hors délai") from exc
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
    """Appelle l'amont et rend sa réponse telle quelle : code, corps et en-têtes utiles.

    Une panne réseau devient 502 (injoignable) ou 504 (délai), avec un message qui ne cite
    ni adresse ni détail de l'erreur. Une redirection n'est pas suivie : elle est rendue.
    """
    url = service.upstream + chemin
    if request.url.query:
        url += "?" + request.url.query
    sortante = httpx.Request(
        request.method, url, headers=await en_tetes_sortants(service, request, email), content=corps,
        extensions={"timeout": http_client.timeout(float(service.timeout_s)).as_dict()},
    )
    try:
        resp = await http_client.get_async_client().send(sortante, stream=True, follow_redirects=False)
        try:
            contenu = await _lire_reponse(service, resp)
        finally:
            await resp.aclose()
    except httpx.TimeoutException as exc:
        raise ToolsError(504, "unavailable", f"{service.label} n'a pas répondu dans le délai") from exc
    except httpx.HTTPError as exc:
        logger.warning("tools: %s injoignable: %s", service.id, exc.__class__.__name__)
        raise ToolsError(502, "unavailable", f"{service.label} est injoignable") from exc
    if resp.status_code == 401 and service.auth_amont and service.auth_amont.kind == "client_credentials":
        _jetons.pop(_cle_jeton(service), None)  # jeton refusé : le prochain appel en redemande un
    suivi = service.follow
    if suivi and request.method == "POST" and chemin in suivi.paths and 200 <= resp.status_code < 300:
        return await _suivre(service, suivi, contenu)
    return Response(
        content=contenu, status_code=resp.status_code,
        headers={nom: resp.headers[nom] for nom in _EN_TETES_REPONSE if nom in resp.headers},
    )


async def _lire_reponse(service: Service, resp: httpx.Response) -> bytes:
    """Corps de la réponse amont, borné comme le corps entrant (`max_body_mb`). Une réponse
    plus grosse n'est pas coupée (elle serait fausse) : elle est refusée."""
    return await _lire_borne(resp, service.max_body_bytes, f"réponse de {service.label} trop volumineuse")


async def _lire_borne(resp: httpx.Response, limite: int, message: str) -> bytes:
    """Corps d'une réponse lu par morceaux ; 502 dès que la limite est dépassée."""
    morceaux: list[bytes] = []
    lu = 0
    async for morceau in resp.aiter_bytes():
        lu += len(morceau)
        if lu > limite:
            raise ToolsError(502, "unavailable", message)
        morceaux.append(morceau)
    return b"".join(morceaux)


def _adresse_a_suivre(suivi: Suivi, contenu: bytes) -> httpx.URL | None:
    """Adresse du fichier rendue par l'amont, ou None si elle n'est pas à suivre : réponse
    illisible, champ absent, autre schéma que https, identifiants dans l'adresse, hôte hors
    de `follow.hosts` (comparaison exacte)."""
    try:
        obj = json.loads(contenu)
        url = httpx.URL(obj[suivi.url_field])
    except (ValueError, KeyError, TypeError, httpx.InvalidURL):
        return None
    hote = url.host if url.port is None else f"{url.host}:{url.port}"
    if url.scheme != "https" or url.userinfo or hote not in suivi.hosts:
        return None
    return url


async def _suivre(service: Service, suivi: Suivi, contenu: bytes) -> Response:
    """Va chercher le fichier dont l'amont vient de rendre l'adresse, et rend son contenu.

    L'adresse est un secret à durée courte (elle porte sa signature) : elle ne sort ni dans
    le journal ni dans une erreur. L'appel au stockage ne porte aucun en-tête du poste ni
    du DM, et ne suit aucune redirection (elle ferait sortir de `follow.hosts`).
    """
    url = _adresse_a_suivre(suivi, contenu)
    if url is None:
        logger.warning("tools: %s, adresse de téléchargement absente ou refusée (https et follow.hosts)", service.id)
        raise ToolsError(502, "unavailable", f"{service.label} a rendu une adresse de téléchargement refusée")
    sortante = httpx.Request(
        "GET", url, headers={"accept-encoding": "identity"},
        extensions={"timeout": http_client.timeout(float(service.timeout_s)).as_dict()},
    )
    try:
        # Délai total, lecture comprise : le délai httpx ne borne que l'attente entre deux morceaux.
        with anyio.fail_after(service.timeout_s):
            resp = await http_client.get_async_client().send(sortante, stream=True, follow_redirects=False)
            try:
                if resp.status_code != 200:
                    # Le corps d'un refus du stockage n'est pas recopié : il peut citer l'adresse.
                    logger.warning("tools: %s, stockage en refus (HTTP %s)", service.id, resp.status_code)
                    raise ToolsError(502, "unavailable", f"le fichier de {service.label} n'a pas pu être téléchargé")
                fichier = await _lire_borne(resp, suivi.max_bytes, f"fichier de {service.label} trop volumineux")
            finally:
                await resp.aclose()
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ToolsError(504, "unavailable", f"le stockage de {service.label} n'a pas répondu dans le délai") from exc
    except httpx.HTTPError as exc:
        logger.warning("tools: %s, stockage injoignable: %s", service.id, exc.__class__.__name__)
        raise ToolsError(502, "unavailable", f"le stockage de {service.label} est injoignable") from exc
    return Response(content=fichier, status_code=200,
                    headers={"content-type": resp.headers.get("content-type", "application/octet-stream")})
