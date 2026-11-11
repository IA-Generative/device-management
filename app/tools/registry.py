"""Registre des services d'outils, lu dans la configuration (DM_TOOLS_REGISTRY, JSON,
relu à chaque requête → hot-reload réel comme les clés du proxy LLM).

Forme :

    {"services": [
      {"id": "mescollections", "label": "Mes collections", "protocol": "mcp",
       "upstream": "https://hote/mcp", "identity": "passthrough",
       "timeout_s": 10, "max_response_kb": 256, "profiles": ["prod"],
       "health_path": "/health",
       "tools": {"rechercher": {"acts": false}, "trouver_collections": {"acts": false}}},
      {"id": "mesreunions", "protocol": "rest", "upstream": "https://hote",
       "tools": {"chercher": {"description": "…", "acts": false, "method": "GET",
                              "path": "/api/v1/search", "query": ["q", "limit", "from", "to"],
                              "parameters": {"type": "object", "properties": {…}, "required": ["q"]}}}}
    ]}

Règles :
- liste blanche stricte : seuls les outils déclarés dans `tools` passent ;
- un service MCP doit être authentifié (`auth` vaut `bearer`, défaut) : `auth: none` le rejette ;
- `identity` : `passthrough` (le jeton de la personne est transmis tel quel) ; `exchange`
  (échange de jeton Keycloak) est accepté dans le registre mais pas encore servi : le service
  est alors écarté avec un avertissement, pour que la V1 ne mente jamais sur l'identité ;
- `profiles` : le service n'est servi que si DM_CONFIG_PROFILE en fait partie (absent = tous).
Les adresses des amonts viennent des valeurs de déploiement, jamais du code.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .. import runtime_config

logger = logging.getLogger("device-management.tools")

PROTOCOLES = ("rest", "mcp")
IDENTITES = ("passthrough", "exchange")
METHODES_REST = ("GET", "POST")
_NOM_OUTIL = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_NOM_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

DEFAUT_TIMEOUT_S = 15
DEFAUT_MAX_RESPONSE_KB = 256
PLAFOND_TIMEOUT_S = 180
PLAFOND_MAX_RESPONSE_KB = 4096


@dataclass(frozen=True)
class Outil:
    name: str
    acts: bool = False
    description: str = ""
    parameters: dict | None = None      # schéma JSON des arguments (REST : requis ; MCP : vient de tools/list)
    method: str = "GET"                 # REST
    path: str = ""                      # REST, peut porter des {param}
    query: tuple[str, ...] = ()         # REST : arguments envoyés en query string
    body: str = ""                      # REST : "json" → arguments restants en corps JSON
    timeout_s: int | None = None        # surcharge du délai du service

    @property
    def schema(self) -> dict:
        return self.parameters or {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class Service:
    id: str
    protocol: str
    upstream: str
    label: str = ""
    identity: str = "passthrough"
    timeout_s: int = DEFAUT_TIMEOUT_S
    max_response_kb: int = DEFAUT_MAX_RESPONSE_KB
    health_path: str = ""
    profiles: tuple[str, ...] = ()
    tools: dict[str, Outil] = field(default_factory=dict)

    def outil(self, name: str) -> Outil | None:
        return self.tools.get(name)

    def timeout_pour(self, outil: Outil) -> float:
        return float(outil.timeout_s or self.timeout_s)

    @property
    def max_response_bytes(self) -> int:
        return int(self.max_response_kb) * 1024


def nom_public(service_id: str, outil: str) -> str:
    """Nom d'outil côté plugin : service__outil (double souligné, jeu OpenAI [a-zA-Z0-9_-])."""
    return f"{service_id}__{outil}"


def _int_borne(valeur, defaut: int, plafond: int, *, minimum: int = 1) -> int:
    try:
        v = int(valeur)
    except (TypeError, ValueError):
        return defaut
    return max(minimum, min(plafond, v))


def _parse_outil(service_id: str, name: str, brut, protocol: str) -> Outil | None:
    if not isinstance(name, str) or not _NOM_OUTIL.match(name):
        logger.warning("tools registry: service %s, nom d'outil invalide ignoré", service_id)
        return None
    if brut is None:
        brut = {}
    if not isinstance(brut, dict):
        logger.warning("tools registry: service %s, outil %s mal formé ignoré", service_id, name)
        return None
    parameters = brut.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        logger.warning("tools registry: service %s, outil %s : parameters n'est pas un objet", service_id, name)
        return None
    method = str(brut.get("method") or "GET").upper()
    path = str(brut.get("path") or "")
    if protocol == "rest":
        if method not in METHODES_REST:
            logger.warning("tools registry: service %s, outil %s : méthode %s refusée", service_id, name, method)
            return None
        if not path.startswith("/"):
            logger.warning("tools registry: service %s, outil %s : path absent", service_id, name)
            return None
        if parameters is None:
            logger.warning("tools registry: service %s, outil %s : schéma parameters requis en REST", service_id, name)
            return None
    query = brut.get("query") or ()
    if not isinstance(query, (list, tuple)) or not all(isinstance(q, str) for q in query):
        query = ()
    timeout_s = brut.get("timeout_s")
    return Outil(
        name=name,
        acts=bool(brut.get("acts", False)),
        description=str(brut.get("description") or ""),
        parameters=parameters,
        method=method,
        path=path,
        query=tuple(query),
        body=str(brut.get("body") or ""),
        timeout_s=_int_borne(timeout_s, 0, PLAFOND_TIMEOUT_S) or None if timeout_s is not None else None,
    )


def _parse_service(brut) -> Service | None:
    if not isinstance(brut, dict):
        return None
    sid = str(brut.get("id") or "").strip()
    if not _NOM_SERVICE.match(sid):
        logger.warning("tools registry: identifiant de service invalide, entrée ignorée")
        return None
    protocol = str(brut.get("protocol") or "").strip().lower()
    if protocol not in PROTOCOLES:
        logger.warning("tools registry: service %s, protocole %r inconnu", sid, protocol)
        return None
    upstream = str(brut.get("upstream") or "").strip().rstrip("/")
    if not upstream.startswith(("http://", "https://")):
        logger.warning("tools registry: service %s, upstream absent ou non http(s)", sid)
        return None
    auth = str(brut.get("auth") or "bearer").strip().lower()
    if auth != "bearer":
        # Garde-fou de l'issue : un service sans authentification ne s'inscrit pas.
        logger.warning("tools registry: service %s refusé, auth=%s (seul bearer est admis)", sid, auth)
        return None
    identity = str(brut.get("identity") or "passthrough").strip().lower()
    if identity not in IDENTITES:
        logger.warning("tools registry: service %s, identity %r inconnue", sid, identity)
        return None
    if identity == "exchange":
        logger.warning("tools registry: service %s écarté, l'échange de jeton n'est pas encore servi", sid)
        return None
    tools_brut = brut.get("tools")
    if not isinstance(tools_brut, dict) or not tools_brut:
        logger.warning("tools registry: service %s sans liste blanche d'outils, ignoré", sid)
        return None
    tools: dict[str, Outil] = {}
    for name, t in tools_brut.items():
        outil = _parse_outil(sid, name, t, protocol)
        if outil is not None:
            tools[name] = outil
    if not tools:
        return None
    profiles = brut.get("profiles") or ()
    if not isinstance(profiles, (list, tuple)):
        profiles = ()
    health_path = str(brut.get("health_path") or "")
    if health_path and not health_path.startswith("/"):
        health_path = ""
    return Service(
        id=sid,
        protocol=protocol,
        upstream=upstream,
        label=str(brut.get("label") or sid),
        identity=identity,
        timeout_s=_int_borne(brut.get("timeout_s"), _defaut_timeout(), PLAFOND_TIMEOUT_S),
        max_response_kb=_int_borne(brut.get("max_response_kb"), _defaut_max_kb(), PLAFOND_MAX_RESPONSE_KB),
        health_path=health_path,
        profiles=tuple(str(p).strip().lower() for p in profiles if str(p).strip()),
        tools=tools,
    )


def _defaut_timeout() -> int:
    return _int_borne(runtime_config.cfg("DM_TOOLS_DEFAULT_TIMEOUT_SECONDS", DEFAUT_TIMEOUT_S),
                      DEFAUT_TIMEOUT_S, PLAFOND_TIMEOUT_S)


def _defaut_max_kb() -> int:
    return _int_borne(runtime_config.cfg("DM_TOOLS_MAX_RESPONSE_KB", DEFAUT_MAX_RESPONSE_KB),
                      DEFAUT_MAX_RESPONSE_KB, PLAFOND_MAX_RESPONSE_KB)


def enabled() -> bool:
    raw = str(runtime_config.cfg("DM_TOOLS_ENABLED", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def profil_courant() -> str:
    return (os.getenv("DM_CONFIG_PROFILE") or "prod").strip().lower()


def parse_registry(raw: str) -> list[Service]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("tools registry: DM_TOOLS_REGISTRY n'est pas du JSON valide, registre vide")
        return []
    items = data.get("services") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    services: list[Service] = []
    vus: set[str] = set()
    for brut in items:
        s = _parse_service(brut)
        if s is None or s.id in vus:
            continue
        vus.add(s.id)
        services.append(s)
    return services


def services_actifs() -> list[Service]:
    """Services servis à ce DM : relais activé, registre valide, profil admis."""
    if not enabled():
        return []
    profil = profil_courant()
    return [
        s for s in parse_registry(str(runtime_config.cfg("DM_TOOLS_REGISTRY", "") or ""))
        if not s.profiles or profil in s.profiles
    ]


def trouver(service_id: str) -> Service | None:
    for s in services_actifs():
        if s.id == service_id:
            return s
    return None
