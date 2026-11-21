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

Troisième protocole, `passthrough` : tout ce qui suit le nom du service est propagé tel
quel vers `base_url` (voir passthrough.py). L'entrée s'écrit dans la liste `services`, ou
directement sous son nom à la racine du registre :

    {"eloignement": {"protocol": "passthrough", "base_url": "http://localhost:8000",
                     "paths": ["/v1/"], "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                     "max_body_mb": 60, "timeout_s": 120,
                     "auth": {"kind": "client_credentials", "token_url": "…", "client_id": "…",
                              "secret_env": "ELOIGNEMENT_CLIENT_SECRET"},
                     "agent_header": "X-Agent-Email"}}

Règles :
- liste blanche stricte : seuls les outils déclarés dans `tools` passent (`rest`, `mcp`) ;
  en `passthrough`, seuls les préfixes de `paths` et les verbes de `methods` passent ;
- un service `passthrough` déclare son authentification amont (`auth.kind` : `client_credentials`,
  `static` ou `header`) ; le secret n'est jamais dans le registre, seulement le NOM de la
  variable d'environnement qui le porte (`secret_env`) ;
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

PROTOCOLES = ("rest", "mcp", "passthrough")
IDENTITES = ("passthrough", "exchange")
METHODES_REST = ("GET", "POST")
METHODES_PASSTHROUGH = ("GET", "POST", "PUT", "PATCH", "DELETE")
AUTHS_AMONT = ("client_credentials", "static", "header")
_NOM_OUTIL = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_NOM_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_NOM_EN_TETE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_NOM_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

DEFAUT_TIMEOUT_S = 15
DEFAUT_MAX_RESPONSE_KB = 256
PLAFOND_TIMEOUT_S = 180
PLAFOND_MAX_RESPONSE_KB = 4096
DEFAUT_MAX_BODY_MB = 10
PLAFOND_MAX_BODY_MB = 100


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
class AuthAmont:
    """Authentification d'un service passthrough auprès de son amont. Le secret est lu à
    l'appel dans la variable d'environnement `secret_env`, jamais gardé ici."""
    kind: str                 # client_credentials | static | header
    secret_env: str
    token_url: str = ""       # client_credentials
    client_id: str = ""       # client_credentials
    header_name: str = ""     # header


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
    # Protocole passthrough uniquement.
    paths: tuple[str, ...] = ()         # préfixes de chemin autorisés
    methods: tuple[str, ...] = ()       # verbes autorisés
    max_body_mb: int = DEFAUT_MAX_BODY_MB
    auth_amont: AuthAmont | None = None
    agent_header: str = ""              # en-tête qui porte l'e-mail de la personne

    def outil(self, name: str) -> Outil | None:
        return self.tools.get(name)

    def timeout_pour(self, outil: Outil) -> float:
        return float(outil.timeout_s or self.timeout_s)

    @property
    def max_response_bytes(self) -> int:
        return int(self.max_response_kb) * 1024

    @property
    def max_body_bytes(self) -> int:
        return int(self.max_body_mb) * 1024 * 1024


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


def _parse_auth_amont(sid: str, brut) -> AuthAmont | None:
    if not isinstance(brut, dict):
        logger.warning("tools registry: service %s refusé, auth absente (objet {kind, secret_env, …} requis)", sid)
        return None
    kind = str(brut.get("kind") or "").strip().lower()
    secret_env = str(brut.get("secret_env") or "").strip()
    if kind not in AUTHS_AMONT or not _NOM_VARIABLE.match(secret_env):
        logger.warning("tools registry: service %s refusé, auth.kind ou auth.secret_env invalide", sid)
        return None
    token_url = str(brut.get("token_url") or "").strip()
    client_id = str(brut.get("client_id") or "").strip()
    header_name = str(brut.get("header_name") or "").strip()
    if kind == "client_credentials" and not (token_url.startswith(("http://", "https://")) and client_id):
        logger.warning("tools registry: service %s refusé, token_url et client_id requis", sid)
        return None
    if kind == "header" and not _NOM_EN_TETE.match(header_name):
        logger.warning("tools registry: service %s refusé, auth.header_name invalide", sid)
        return None
    return AuthAmont(kind=kind, secret_env=secret_env, token_url=token_url, client_id=client_id,
                     header_name=header_name)


def _parse_passthrough(sid: str, brut: dict) -> Service | None:
    base_url = str(brut.get("base_url") or brut.get("upstream") or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        logger.warning("tools registry: service %s, base_url absente ou non http(s)", sid)
        return None
    paths = brut.get("paths")
    if (not isinstance(paths, list) or not paths
            or not all(isinstance(p, str) and p.startswith("/") and ".." not in p for p in paths)):
        logger.warning("tools registry: service %s refusé, paths doit lister des préfixes commençant par /", sid)
        return None
    methods = brut.get("methods")
    if not isinstance(methods, list) or not methods:
        logger.warning("tools registry: service %s refusé, methods doit lister les verbes autorisés", sid)
        return None
    methods = tuple(str(m).strip().upper() for m in methods)
    if not all(m in METHODES_PASSTHROUGH for m in methods):
        logger.warning("tools registry: service %s refusé, verbe hors %s", sid, "/".join(METHODES_PASSTHROUGH))
        return None
    auth_amont = _parse_auth_amont(sid, brut.get("auth"))
    if auth_amont is None:
        return None
    agent_header = str(brut.get("agent_header") or "").strip()
    if agent_header and not _NOM_EN_TETE.match(agent_header):
        logger.warning("tools registry: service %s refusé, agent_header invalide", sid)
        return None
    return Service(
        id=sid,
        protocol="passthrough",
        upstream=base_url,
        label=str(brut.get("label") or sid),
        timeout_s=_int_borne(brut.get("timeout_s"), _defaut_timeout(), PLAFOND_TIMEOUT_S),
        health_path=_health_path(brut),
        profiles=_profiles(brut),
        paths=tuple(paths),
        methods=methods,
        max_body_mb=_int_borne(brut.get("max_body_mb"), DEFAUT_MAX_BODY_MB, PLAFOND_MAX_BODY_MB),
        auth_amont=auth_amont,
        agent_header=agent_header,
    )


def _profiles(brut: dict) -> tuple[str, ...]:
    profiles = brut.get("profiles") or ()
    if not isinstance(profiles, (list, tuple)):
        return ()
    return tuple(str(p).strip().lower() for p in profiles if str(p).strip())


def _health_path(brut: dict) -> str:
    health_path = str(brut.get("health_path") or "")
    return health_path if health_path.startswith("/") else ""


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
    if protocol == "passthrough":
        return _parse_passthrough(sid, brut)
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
    return Service(
        id=sid,
        protocol=protocol,
        upstream=upstream,
        label=str(brut.get("label") or sid),
        identity=identity,
        timeout_s=_int_borne(brut.get("timeout_s"), _defaut_timeout(), PLAFOND_TIMEOUT_S),
        max_response_kb=_int_borne(brut.get("max_response_kb"), _defaut_max_kb(), PLAFOND_MAX_RESPONSE_KB),
        health_path=_health_path(brut),
        profiles=_profiles(brut),
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
    if isinstance(data, dict):
        items = data.get("services") or []
        if isinstance(items, list):
            # Forme du contrat Éloignement : une entrée directement sous le nom du service.
            items = items + [{**v, "id": k} for k, v in data.items() if k != "services" and isinstance(v, dict)]
    else:
        items = data
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
