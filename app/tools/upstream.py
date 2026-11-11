"""Appels amont du relais d'outils : REST (route déclarée) et MCP Streamable HTTP
(JSON-RPC `tools/list` et `tools/call`), avec borne de taille et délai du registre.

Identité : le jeton Keycloak de la personne est transmis tel quel (`Authorization:
Bearer`). Aucun autre en-tête entrant ne passe (ni relay, ni cookie). La réponse amont
est lue au plus jusqu'à `max_response_bytes` + 1 : au-delà, elle est coupée et signalée.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from . import http_client
from .errors import ToolsError, code_amont
from .registry import Outil, Service

logger = logging.getLogger("device-management.tools")

_ACCEPT_MCP = "application/json, text/event-stream"


@dataclass
class ReponseAmont:
    status: int
    body: bytes
    truncated: bool
    content_type: str
    retry_after: int | None


def _retry_after(headers: httpx.Headers) -> int | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return 60


async def _envoyer(service: Service, outil: Outil, request: httpx.Request) -> ReponseAmont:
    client = http_client.get_async_client()
    limite = service.max_response_bytes
    try:
        resp = await client.send(request, stream=True, follow_redirects=False)
    except httpx.TimeoutException as exc:
        raise ToolsError(504, "unavailable", f"{service.label} n'a pas répondu dans le délai") from exc
    except httpx.HTTPError as exc:
        logger.warning("tools: %s injoignable: %s", service.id, exc.__class__.__name__)
        raise ToolsError(502, "unavailable", f"{service.label} est injoignable") from exc
    try:
        morceaux: list[bytes] = []
        lu = 0
        truncated = False
        async for chunk in resp.aiter_bytes():
            if lu + len(chunk) > limite:
                morceaux.append(chunk[: max(0, limite - lu)])
                lu = limite
                truncated = True
                break
            morceaux.append(chunk)
            lu += len(chunk)
    except httpx.TimeoutException as exc:
        raise ToolsError(504, "unavailable", f"{service.label} a cessé de répondre") from exc
    except httpx.HTTPError as exc:
        raise ToolsError(502, "unavailable", f"lecture de {service.label} interrompue") from exc
    finally:
        await resp.aclose()
    return ReponseAmont(
        status=resp.status_code,
        body=b"".join(morceaux),
        truncated=truncated,
        content_type=resp.headers.get("content-type", ""),
        retry_after=_retry_after(resp.headers),
    )


def _lever_si_erreur_amont(service: Service, rep: ReponseAmont) -> None:
    """Un statut amont ≥ 400 devient une erreur du contrat ; le message amont est repris
    s'il a la forme {error:{code,message}}, sans jamais recopier d'autre contenu."""
    if rep.status < 400:
        return
    code = code_amont(rep.status)
    message = f"{service.label} a répondu {rep.status}"
    try:
        obj = json.loads(rep.body.decode("utf-8", "replace")) if rep.body and not rep.truncated else None
    except ValueError:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        if isinstance(err.get("message"), str) and err["message"].strip():
            message = err["message"].strip()[:300]
        if rep.status == 403 and isinstance(err.get("code"), str) and err["code"] == "audience_mismatch":
            message = f"{service.label} refuse l'audience du jeton (audience_mismatch)"
    statut = rep.status if rep.status in (400, 401, 403, 404, 413, 422, 429) else (503 if rep.status >= 500 else 400)
    raise ToolsError(statut, code, message, retry_after=rep.retry_after if rep.status in (429, 503) else None)


def _decoder_json(service: Service, rep: ReponseAmont):
    """JSON de la réponse ; une réponse coupée rend son texte partiel, signalé."""
    texte = rep.body.decode("utf-8", "replace")
    if rep.truncated:
        return {"text": texte}, True
    if not texte.strip():
        return None, False
    try:
        return json.loads(texte), False
    except ValueError:
        return {"text": texte}, False


# ── REST ──────────────────────────────────────────────────────────────────────

def _construire_rest(service: Service, outil: Outil, arguments: dict, bearer: str) -> httpx.Request:
    restants = dict(arguments)
    path = outil.path
    for cle in list(restants):
        marque = "{" + cle + "}"
        if marque in path:
            path = path.replace(marque, quote(str(restants.pop(cle)), safe=""))
    if "{" in path:
        raise ToolsError(400, "invalid_arguments", "un paramètre de chemin manque")
    params = {}
    for cle in outil.query:
        if cle in restants and restants[cle] is not None:
            v = restants.pop(cle)
            params[cle] = json.dumps(v) if isinstance(v, (dict, list)) else (str(v).lower() if isinstance(v, bool) else v)
    headers = {"Authorization": bearer, "Accept": "application/json"}
    contenu = None
    if outil.method == "POST" and (outil.body == "json" or restants):
        headers["Content-Type"] = "application/json"
        contenu = json.dumps(restants, ensure_ascii=False).encode("utf-8")
    return httpx.Request(
        outil.method, f"{service.upstream}{path}", params=params or None, headers=headers, content=contenu,
        extensions={"timeout": http_client.timeout(service.timeout_pour(outil)).as_dict()},
    )


async def appeler_rest(service: Service, outil: Outil, arguments: dict, bearer: str) -> tuple[object, bool, int]:
    rep = await _envoyer(service, outil, _construire_rest(service, outil, arguments, bearer))
    _lever_si_erreur_amont(service, rep)
    result, truncated = _decoder_json(service, rep)
    return result, truncated, len(rep.body)


# ── MCP Streamable HTTP (sans état, réponse JSON ou SSE) ───────────────────────

def _construire_mcp(service: Service, outil: Outil | None, method: str, params: dict, bearer: str,
                    rpc_id: int = 1) -> httpx.Request:
    corps = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params:
        corps["params"] = params
    delai = service.timeout_pour(outil) if outil is not None else float(service.timeout_s)
    return httpx.Request(
        "POST", service.upstream,
        headers={"Authorization": bearer, "Content-Type": "application/json", "Accept": _ACCEPT_MCP},
        content=json.dumps(corps, ensure_ascii=False).encode("utf-8"),
        extensions={"timeout": http_client.timeout(delai).as_dict()},
    )


def _message_rpc(service: Service, rep: ReponseAmont) -> dict:
    """Extrait le message JSON-RPC d'une réponse JSON simple ou d'un flux SSE."""
    texte = rep.body.decode("utf-8", "replace")
    if "text/event-stream" in rep.content_type.lower():
        dernier = None
        for ligne in texte.splitlines():
            if ligne.startswith("data:"):
                brut = ligne[5:].strip()
                if not brut:
                    continue
                try:
                    obj = json.loads(brut)
                except ValueError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    dernier = obj
        if dernier is None:
            raise ToolsError(502, "unavailable", f"{service.label} : flux MCP sans réponse")
        return dernier
    try:
        obj = json.loads(texte)
    except ValueError as exc:
        raise ToolsError(502, "unavailable", f"{service.label} : réponse MCP illisible") from exc
    if not isinstance(obj, dict):
        raise ToolsError(502, "unavailable", f"{service.label} : réponse MCP inattendue")
    return obj


def _lever_si_erreur_rpc(service: Service, msg: dict) -> None:
    err = msg.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        message = str(err.get("message") or "erreur MCP")[:300]
        if code == -32602:
            raise ToolsError(400, "invalid_arguments", message)
        if code == -32601:
            raise ToolsError(404, "not_found", message)
        raise ToolsError(503, "unavailable", f"{service.label} : {message}")


async def lister_mcp(service: Service, bearer: str) -> list[dict]:
    """tools/list → liste brute des outils déclarés par l'amont (name, description, inputSchema)."""
    rep = await _envoyer(service, next(iter(service.tools.values())),
                         _construire_mcp(service, None, "tools/list", {}, bearer))
    _lever_si_erreur_amont(service, rep)
    if rep.truncated:
        raise ToolsError(502, "unavailable", f"{service.label} : liste d'outils trop volumineuse")
    msg = _message_rpc(service, rep)
    _lever_si_erreur_rpc(service, msg)
    result = msg.get("result") or {}
    outils = result.get("tools") if isinstance(result, dict) else None
    return [t for t in (outils or []) if isinstance(t, dict) and isinstance(t.get("name"), str)]


async def appeler_mcp(service: Service, outil: Outil, arguments: dict, bearer: str) -> tuple[object, bool, int]:
    rep = await _envoyer(service, outil, _construire_mcp(
        service, outil, "tools/call", {"name": outil.name, "arguments": arguments}, bearer, rpc_id=2))
    _lever_si_erreur_amont(service, rep)
    if rep.truncated:
        return {"text": rep.body.decode("utf-8", "replace")}, True, len(rep.body)
    msg = _message_rpc(service, rep)
    _lever_si_erreur_rpc(service, msg)
    result = msg.get("result")
    if not isinstance(result, dict):
        raise ToolsError(502, "unavailable", f"{service.label} : résultat MCP inattendu")
    contenu = result.get("content") or []
    textes = [c.get("text") for c in contenu if isinstance(c, dict) and c.get("type") == "text"]
    if result.get("isError"):
        # Une erreur d'outil MCP (HTTP 200 + isError) devient une erreur du relais, pas un 200.
        message = (textes[0] if textes and isinstance(textes[0], str) else "l'outil a échoué")[:300]
        raise ToolsError(400, "invalid_arguments" if "argument" in message.lower() else "blocked", message)
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"], False, len(rep.body)
    if textes and isinstance(textes[0], str):
        try:
            return json.loads(textes[0]), False, len(rep.body)
        except ValueError:
            return {"text": textes[0]}, False, len(rep.body)
    return result, False, len(rep.body)
