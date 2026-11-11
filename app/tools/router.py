"""Routes du relais d'outils (/tools/v1).

- ``GET  /tools/v1`` : découverte, les outils que ce poste peut appeler, au format outils
  OpenAI : {"tools": [{"type": "function", "function": {name, description, parameters},
  "service": "<id>", "acts": bool}]}. Nom public ``service__outil``. Un service coupé ou en
  panne n'est pas listé ; les autres restent.
- ``POST /tools/v1/{service}/{outil}`` : corps {"arguments": {…}} → 200 {"result": <JSON>,
  "truncated": bool}. Erreurs : statut HTTP + {"error": {"code", "message"}}.

Identité, double comme sur /llm/v1 :
- le POSTE par ``X-Relay-Client`` / ``X-Relay-Key`` (cible ``tools``) ou ``Authorization:
  Bearer <llmToken>`` ;
- la PERSONNE par ``X-User-Authorization: Bearer <jeton Keycloak>``, vérifié (signature,
  expiration, émetteur) puis transmis TEL QUEL à l'amont. Le relais n'appelle jamais en son
  nom : pas de clé de service.

``build_router`` reçoit les helpers de app.main par injection (pas d'import circulaire).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import anyio.to_thread
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import runtime_config
from ..llm.auth import _client_uuid_is_active
from ..llm.tokens import verify_llm_token
from . import audit, throttle, upstream
from .errors import ToolsError, tools_error
from .registry import Outil, Service, nom_public, services_actifs, trouver
from .schema import erreurs_arguments

logger = logging.getLogger("device-management.tools")

EN_TETE_PERSONNE = "x-user-authorization"
_CACHE_DECOUVERTE_S = 60.0
_cache_mcp: dict[str, tuple[float, list[dict]]] = {}


@dataclass(frozen=True)
class Identite:
    client_uuid: str
    email: str
    sub: str
    bearer: str          # « Bearer <jeton> » de la personne, transmis tel quel
    auth_method: str


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "") or uuid.uuid4().hex


def public_tools_url() -> str:
    explicit = str(runtime_config.cfg("PUBLIC_TOOLS_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    base = str(runtime_config.cfg("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base}/tools/v1" if base else ""


def _reset_cache_for_tests() -> None:
    _cache_mcp.clear()


def _schema_mcp(t: dict) -> dict:
    s = t.get("inputSchema")
    return s if isinstance(s, dict) else {"type": "object", "properties": {}}


class Authentificateur:
    def __init__(self, relay_auth: Callable[..., tuple[bool, dict | str]], verify_access_token: Callable[[str], dict]):
        self._relay_auth = relay_auth
        self._verify = verify_access_token

    async def poste(self, request: Request) -> tuple[str, str, str]:
        relay_client = (request.headers.get("x-relay-client") or request.headers.get("x-client-id") or "").strip()
        if relay_client:
            ok, meta = await anyio.to_thread.run_sync(lambda: self._relay_auth(request, target="tools"))
            if not ok:
                raise ToolsError(401, "unauthorized", f"authentification du poste refusée : {meta}")
            assert isinstance(meta, dict)
            return str(meta.get("client_uuid") or ""), str(meta.get("email") or ""), "relay_headers"
        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer ") and auth[7:].strip():
            try:
                payload = verify_llm_token(auth[7:].strip())
            except Exception as exc:  # LlmProxyError → forme du contrat
                raise ToolsError(401, "unauthorized", "llmToken invalide ou expiré") from exc
            client_uuid = str(payload.get("client_uuid") or "")
            if not await anyio.to_thread.run_sync(_client_uuid_is_active, client_uuid):
                raise ToolsError(401, "unauthorized", "poste révoqué ou expiré")
            return client_uuid, str(payload.get("email") or ""), "llm_token"
        raise ToolsError(401, "unauthorized",
                         "identité du poste absente : X-Relay-Client/X-Relay-Key ou Authorization: Bearer <llmToken>")

    async def personne(self, request: Request) -> tuple[str, str]:
        brut = (request.headers.get(EN_TETE_PERSONNE) or "").strip()
        if not brut.lower().startswith("bearer ") or not brut[7:].strip():
            raise ToolsError(401, "unauthorized", "jeton de la personne absent (X-User-Authorization: Bearer …)")
        jeton = brut[7:].strip()
        try:
            payload = await anyio.to_thread.run_sync(self._verify, jeton)
        except Exception as exc:
            status = getattr(exc, "status_code", 401)
            if status == 503:
                raise ToolsError(503, "unavailable", "vérification du jeton indisponible") from exc
            raise ToolsError(401, "unauthorized", "jeton de la personne invalide ou expiré") from exc
        sub = str((payload or {}).get("sub") or "")
        if not sub:
            raise ToolsError(401, "unauthorized", "jeton de la personne sans sujet")
        return sub, f"Bearer {jeton}"

    async def resoudre(self, request: Request) -> Identite:
        client_uuid, email, methode = await self.poste(request)
        sub, bearer = await self.personne(request)
        return Identite(client_uuid=client_uuid, email=email, sub=sub, bearer=bearer, auth_method=methode)


async def _outils_du_service(service: Service, bearer: str) -> list[dict]:
    """Outils d'un service au format OpenAI ; MCP : tools/list (cache 60 s), filtré par la liste blanche."""
    if service.protocol == "rest":
        return [{
            "type": "function",
            "function": {"name": nom_public(service.id, o.name), "description": o.description, "parameters": o.schema},
            "service": service.id,
            "acts": o.acts,
        } for o in service.tools.values()]
    now = time.monotonic()
    cache = _cache_mcp.get(service.id)
    if cache and now - cache[0] < _CACHE_DECOUVERTE_S:
        bruts = cache[1]
    else:
        bruts = await upstream.lister_mcp(service, bearer)
        _cache_mcp[service.id] = (now, bruts)
    par_nom = {t["name"]: t for t in bruts}
    sortie = []
    for o in service.tools.values():
        t = par_nom.get(o.name)
        if t is None:
            continue  # déclaré chez nous mais absent de l'amont : pas servi
        sortie.append({
            "type": "function",
            "function": {
                "name": nom_public(service.id, o.name),
                "description": o.description or str(t.get("description") or ""),
                "parameters": _schema_mcp(t),
            },
            "service": service.id,
            "acts": o.acts,
        })
    return sortie


async def _schema_effectif(service: Service, outil: Outil, bearer: str) -> dict:
    if service.protocol == "rest":
        return outil.schema
    for t in await _outils_du_service(service, bearer):
        if t["function"]["name"] == nom_public(service.id, outil.name):
            return t["function"]["parameters"]
    raise ToolsError(404, "not_found", f"l'outil {outil.name} n'est pas servi par {service.label}")


def build_router(
    *,
    relay_auth: Callable[..., tuple[bool, dict | str]],
    verify_access_token: Callable[[str], dict],
) -> APIRouter:
    router = APIRouter(prefix="/tools/v1", tags=["tools-relay"])
    auth = Authentificateur(relay_auth, verify_access_token)

    @router.get("")
    @router.get("/")
    async def decouverte(request: Request) -> Response:
        try:
            identite = await auth.resoudre(request)
        except ToolsError as exc:
            return exc.to_response()
        outils: list[dict] = []
        services = services_actifs()
        for service in services:
            try:
                outils.extend(await _outils_du_service(service, identite.bearer))
            except ToolsError as exc:
                # Panne d'un service : il disparaît de la découverte, les autres restent.
                logger.info("tools: %s absent de la découverte (%s)", service.id, exc.code)
            except Exception:
                logger.exception("tools: découverte de %s en échec", service.id)
        return JSONResponse({"tools": outils, "services": [s.id for s in services]})

    @router.post("/{service_id}/{outil_nom}")
    async def appel(service_id: str, outil_nom: str, request: Request) -> Response:
        t0 = time.perf_counter()
        trace_id = _trace_id(request)
        identite: Identite | None = None
        service = trouver(service_id)
        outil = service.outil(outil_nom) if service else None

        def journal(status: int, *, size: int = 0, error: str | None = None, truncated: bool = False) -> None:
            audit.log_appel(
                trace_id=trace_id, service=service_id, outil=outil_nom, status=status,
                duration_ms=(time.perf_counter() - t0) * 1000, size=size,
                client_uuid=identite.client_uuid if identite else "", sub=identite.sub if identite else "",
                acts=bool(outil and outil.acts), error=error, truncated=truncated,
            )

        try:
            identite = await auth.resoudre(request)
            if service is None or outil is None:
                # Hors liste blanche : refus sans appel amont (même message pour service et outil inconnus).
                raise ToolsError(404, "not_found", "outil inconnu ou non autorisé sur ce service")
            autorise, retry_after = throttle.autoriser(identite.sub, service.id)
            if not autorise:
                raise ToolsError(429, "rate_limited", "trop d'appels pour cette personne sur ce service",
                                 retry_after=retry_after)
            corps = await request.body()
            try:
                payload = json.loads(corps.decode("utf-8")) if corps.strip() else {}
            except ValueError as exc:
                raise ToolsError(400, "invalid_arguments", "corps JSON illisible") from exc
            if not isinstance(payload, dict):
                raise ToolsError(400, "invalid_arguments", "le corps doit être un objet {\"arguments\": {…}}")
            arguments = payload.get("arguments", {})
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ToolsError(400, "invalid_arguments", "arguments doit être un objet")
            schema = await _schema_effectif(service, outil, identite.bearer)
            fautes = erreurs_arguments(schema, arguments)
            if fautes:
                raise ToolsError(400, "invalid_arguments", "arguments refusés : " + " ; ".join(fautes[:5]))
            if service.protocol == "mcp":
                result, truncated, size = await upstream.appeler_mcp(service, outil, arguments, identite.bearer)
            else:
                result, truncated, size = await upstream.appeler_rest(service, outil, arguments, identite.bearer)
        except ToolsError as exc:
            journal(exc.status_code, error=exc.code)
            return exc.to_response()
        except Exception:
            logger.exception("tools: appel %s/%s en échec", service_id, outil_nom)
            journal(500, error="internal")
            return tools_error(500, "unavailable", "erreur interne du relais")
        journal(200, size=size, truncated=truncated)
        return JSONResponse({"result": result, "truncated": truncated})

    return router
