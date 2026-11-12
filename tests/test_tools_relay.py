"""Relais d'outils /tools/v1 (issue #116) : découverte, appel REST et MCP, identité de la
personne transmise telle quelle, liste blanche, bornes, journal sans contenu, pannes isolées.
"""
from __future__ import annotations

import json
import logging

import httpx
from fastapi.testclient import TestClient

from tests.llm_utils import enroll, load_module, mk_fake_jwt

REUNIONS = "https://reunions.test"
COLLECTIONS = "https://collections.test/mcp"

REGISTRE = {
    "services": [
        {
            "id": "mesreunions", "label": "Mes réunions", "protocol": "rest", "upstream": REUNIONS,
            "health_path": "/health", "timeout_s": 7, "max_response_kb": 1,
            "tools": {
                "chercher": {
                    "description": "Recherche dans les réunions de la personne", "acts": False,
                    "method": "GET", "path": "/api/v1/search", "query": ["q", "limit"],
                    "parameters": {
                        "type": "object", "additionalProperties": False, "required": ["q"],
                        "properties": {"q": {"type": "string", "minLength": 1, "maxLength": 1000},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                    },
                },
            },
        },
        {
            "id": "mescollections", "label": "Mes collections", "protocol": "mcp", "upstream": COLLECTIONS,
            "tools": {"rechercher": {"acts": False}, "trouver_collections": {"acts": False}},
        },
        {
            "id": "mesagents", "label": "Mes agents", "protocol": "rest", "upstream": "https://agents.test",
            "profiles": ["int"],
            "tools": {"lancer": {"acts": True, "method": "POST", "path": "/v1/chat/completions", "body": "json",
                                 "parameters": {"type": "object", "properties": {"model": {"type": "string"}}}}},
        },
    ]
}

OUTILS_MCP_AMONT = [
    {"name": "rechercher", "description": "Cherche dans les collections",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"],
                     "additionalProperties": False}},
    {"name": "trouver_collections", "description": "Trouve des collections",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}}},
    {"name": "deposer", "description": "Hors liste blanche", "inputSchema": {"type": "object"}},
]


class Amonts:
    """Faux amonts : enregistrent les requêtes, répondent selon le sub du jeton de la personne."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.panne_reunions = False
        self.redirection_reunions = False
        self.reponse_longue = False
        self.statut_collections = 200
        self.is_error = False

    def _sub(self, request: httpx.Request) -> str:
        auth = request.headers.get("authorization", "")
        try:
            payload = auth.split(" ", 1)[1].split(".")[1]
            payload += "=" * (-len(payload) % 4)
            import base64
            return json.loads(base64.urlsafe_b64decode(payload)).get("sub", "")
        except Exception:
            return ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "reunions.test":
            if self.panne_reunions:
                raise httpx.ConnectError("refus", request=request)
            if request.url.path == "/api/v1/search" and not self.redirection_reunions:
                if self.reponse_longue:
                    return httpx.Response(200, json={"results": ["x" * 3000]})
                return httpx.Response(200, json={"total": 1, "query": request.url.params.get("q"),
                                                 "results": [{"id": f"r-{self._sub(request)}"}]})
            return httpx.Response(302, headers={"location": "/login"})
        if host == "collections.test":
            if self.statut_collections != 200:
                return httpx.Response(self.statut_collections, json={
                    "error": {"code": "audience_mismatch", "message": "mauvaise audience"}})
            msg = json.loads(request.content)
            if msg["method"] == "tools/list":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": OUTILS_MCP_AMONT}})
            if msg["method"] == "tools/call":
                if self.is_error:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "isError": True, "content": [{"type": "text", "text": "collection introuvable"}]}})
                texte = json.dumps({"outil": msg["params"]["name"], "sub": self._sub(request),
                                    "question": msg["params"]["arguments"].get("question")})
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                                 "result": {"content": [{"type": "text", "text": texte}]}})
        return httpx.Response(404, json={"error": "inconnu"})

    def install(self):
        from app.tools import http_client
        http_client.set_transport_for_tests(httpx.MockTransport(self.handler))


def _setup(extra_env=None, registre=REGISTRE):
    env = {"DM_TOOLS_ENABLED": "true", "DM_TOOLS_REGISTRY": json.dumps(registre), "DM_TOOLS_RATE_PER_MINUTE": "0"}
    if extra_env:
        env.update(extra_env)
    mod = load_module(env)
    from app.tools import router as tools_router
    from app.tools import throttle
    throttle.reset_for_tests()
    tools_router._reset_cache_for_tests()
    amonts = Amonts()
    amonts.install()
    return mod, TestClient(mod.app), amonts


def _personne(sub="alice", email="alice@example.com") -> dict[str, str]:
    return {"X-User-Authorization": f"Bearer {mk_fake_jwt({'sub': sub, 'email': email, 'exp': 4102444800})}"}


def _entetes(client, sub="alice") -> dict[str, str]:
    h = enroll(client)
    h.update(_personne(sub))
    return h


# ── Découverte ────────────────────────────────────────────────────────────────

def test_decouverte_liste_rest_et_mcp_avec_schemas():
    _, client, amonts = _setup()
    res = client.get("/tools/v1", headers=_entetes(client))
    assert res.status_code == 200, res.text
    body = res.json()
    noms = {t["function"]["name"]: t for t in body["tools"]}
    assert set(noms) == {"mesreunions__chercher", "mescollections__rechercher", "mescollections__trouver_collections"}
    assert noms["mesreunions__chercher"]["function"]["parameters"]["required"] == ["q"]
    assert noms["mesreunions__chercher"]["acts"] is False
    assert noms["mesreunions__chercher"]["service"] == "mesreunions"
    # Le schéma MCP vient de tools/list de l'amont ; « deposer » est hors liste blanche.
    assert noms["mescollections__rechercher"]["function"]["parameters"]["required"] == ["question"]
    assert "mescollections__deposer" not in noms
    # Profil prod : « mesagents » (profils ["int"]) n'est ni listé…
    assert "mesagents" not in body["services"]
    # …et tools/list a bien porté le jeton de la personne.
    liste = [r for r in amonts.requests if r.url.host == "collections.test"]
    assert liste and liste[0].headers["authorization"].startswith("Bearer ")


def test_decouverte_sans_identite_de_personne_401():
    _, client, amonts = _setup()
    res = client.get("/tools/v1", headers=enroll(client))
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"
    assert amonts.requests == []


def test_decouverte_sans_identite_de_poste_401():
    _, client, amonts = _setup()
    res = client.get("/tools/v1", headers=_personne())
    assert res.status_code == 401
    assert amonts.requests == []


def test_service_en_panne_disparait_les_autres_restent():
    _, client, amonts = _setup()
    amonts.statut_collections = 503
    res = client.get("/tools/v1", headers=_entetes(client))
    assert res.status_code == 200
    noms = {t["function"]["name"] for t in res.json()["tools"]}
    assert noms == {"mesreunions__chercher"}


# ── Appel REST ────────────────────────────────────────────────────────────────

def test_appel_rest_porte_l_identite_de_la_personne():
    _, client, amonts = _setup()
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client, "alice"),
                      json={"arguments": {"q": "budget", "limit": 5}})
    assert res.status_code == 200, res.text
    assert res.json() == {"result": {"total": 1, "query": "budget", "results": [{"id": "r-alice"}]}, "truncated": False}
    sortant = amonts.requests[-1]
    assert sortant.method == "GET"
    assert sortant.url.path == "/api/v1/search"
    assert sortant.url.params["q"] == "budget" and sortant.url.params["limit"] == "5"
    assert sortant.headers["authorization"].startswith("Bearer ")
    # Les identifiants du poste ne sortent pas.
    for h in ("x-relay-client", "x-relay-key", "x-user-authorization", "cookie"):
        assert h not in sortant.headers
    # Deux personnes n'obtiennent pas les mêmes résultats.
    res2 = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client, "bob"),
                       json={"arguments": {"q": "budget"}})
    assert res2.json()["result"]["results"] == [{"id": "r-bob"}]


def test_hors_liste_blanche_refuse_sans_appel_amont():
    _, client, amonts = _setup()
    for chemin in ("/tools/v1/mesreunions/supprimer", "/tools/v1/inconnu/chercher", "/tools/v1/mesagents/lancer"):
        res = client.post(chemin, headers=_entetes(client), json={"arguments": {}})
        assert res.status_code == 404, chemin
        assert res.json()["error"]["code"] == "not_found"
    assert amonts.requests == []


def test_arguments_invalides_refuses_sans_appel_amont():
    _, client, amonts = _setup()
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client),
                      json={"arguments": {"limit": 5}})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_arguments"
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client),
                      json={"arguments": {"q": "x", "autre": 1}})
    assert res.status_code == 400
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client), content=b"pas du json")
    assert res.status_code == 400
    assert amonts.requests == []


def test_reponse_trop_grande_coupee_et_signalee():
    _, client, amonts = _setup()
    amonts.reponse_longue = True
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client), json={"arguments": {"q": "x"}})
    assert res.status_code == 200
    body = res.json()
    assert body["truncated"] is True
    assert len(body["result"]["text"]) <= 1024


def test_amont_injoignable_rend_unavailable():
    _, client, amonts = _setup()
    amonts.panne_reunions = True
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client), json={"arguments": {"q": "x"}})
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "unavailable"


def test_debit_par_personne_et_service():
    _, client, _ = _setup({"DM_TOOLS_RATE_PER_MINUTE": "1"})
    h = _entetes(client, "alice")
    assert client.post("/tools/v1/mesreunions/chercher", headers=h, json={"arguments": {"q": "x"}}).status_code == 200
    res = client.post("/tools/v1/mesreunions/chercher", headers=h, json={"arguments": {"q": "x"}})
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "rate_limited"
    assert int(res.headers["Retry-After"]) >= 1
    # Un autre service, et une autre personne, ne sont pas comptés avec.
    assert client.post("/tools/v1/mescollections/rechercher", headers=h,
                       json={"arguments": {"question": "x"}}).status_code == 200
    assert client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client, "bob"),
                       json={"arguments": {"q": "x"}}).status_code == 200


# ── Appel MCP ─────────────────────────────────────────────────────────────────

def test_appel_mcp_deballe_le_texte_json():
    _, client, amonts = _setup()
    res = client.post("/tools/v1/mescollections/rechercher", headers=_entetes(client, "alice"),
                      json={"arguments": {"question": "procédure"}})
    assert res.status_code == 200, res.text
    assert res.json() == {"result": {"outil": "rechercher", "sub": "alice", "question": "procédure"}, "truncated": False}
    appel = [r for r in amonts.requests if json.loads(r.content).get("method") == "tools/call"][-1]
    assert appel.headers["accept"].startswith("application/json")
    assert json.loads(appel.content)["params"] == {"name": "rechercher", "arguments": {"question": "procédure"}}


def test_erreur_d_outil_mcp_n_est_pas_un_200():
    _, client, amonts = _setup()
    amonts.is_error = True
    res = client.post("/tools/v1/mescollections/rechercher", headers=_entetes(client),
                      json={"arguments": {"question": "x"}})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "upstream_error"
    assert res.json()["error"]["message"] == "collection introuvable"


def test_redirection_amont_n_est_pas_un_200():
    """Un 302 vers /login (HTML) n'est ni suivi ni pris pour une acceptation."""
    _, client, amonts = _setup()
    amonts.redirection_reunions = True
    res = client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client),
                      json={"arguments": {"q": "budget"}})
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "unavailable"
    assert "redirection" in res.json()["error"]["message"]
    assert all(r.url.path != "/login" for r in amonts.requests)


def test_refus_amont_403_devient_forbidden():
    _, client, amonts = _setup()
    amonts.statut_collections = 403
    res = client.post("/tools/v1/mescollections/rechercher", headers=_entetes(client),
                      json={"arguments": {"question": "x"}})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"
    assert "audience_mismatch" in res.json()["error"]["message"]


# ── Journal, config, modes ────────────────────────────────────────────────────

def test_journal_sans_arguments_ni_contenu(caplog):
    _, client, _ = _setup()
    with caplog.at_level(logging.INFO, logger="dm-tools-audit"):
        client.post("/tools/v1/mesreunions/chercher", headers=_entetes(client, "alice"),
                    json={"arguments": {"q": "mot-secret-de-recherche"}})
    lignes = [r.getMessage() for r in caplog.records if r.name == "dm-tools-audit"]
    assert lignes, "une ligne d'audit par appel"
    entree = json.loads(lignes[-1])
    assert entree["service"] == "mesreunions" and entree["tool"] == "chercher" and entree["status"] == 200
    assert "mot-secret-de-recherche" not in lignes[-1]
    assert "r-alice" not in lignes[-1]
    assert "alice" not in entree.get("sub", "")  # sub haché, jamais en clair


def test_config_annonce_toolsendpoint_quand_actif():
    _, client, _ = _setup()
    res = client.get("/config/libreoffice/config.json?profile=prod", headers=enroll(client))
    assert res.status_code == 200
    assert res.json()["config"]["toolsEndpoint"] == "https://example.test/bootstrap/tools/v1"


def test_config_toolsendpoint_vide_quand_desactive():
    _, client, _ = _setup({"DM_TOOLS_ENABLED": "false"})
    res = client.get("/config/libreoffice/config.json?profile=prod", headers=enroll(client))
    assert res.json()["config"]["toolsEndpoint"] == ""
    assert client.get("/tools/v1", headers=_entetes(client)).json()["tools"] == []


def test_mode_llm_sert_le_relais_d_outils():
    mod = load_module({"DM_RUNTIME_MODE": "llm", "DM_TOOLS_ENABLED": "true", "DM_TOOLS_REGISTRY": json.dumps(REGISTRE)})
    client = TestClient(mod.app)
    assert client.get("/tools/v1").status_code == 401  # la route répond (401 sans identité)


def test_identite_du_poste_par_llmtoken():
    mod, client, _ = _setup()
    from app.llm.tokens import mint_llm_token
    enroll(client)
    client_uuid = next(iter(mod._RELAY_MEMORY_STORE.values()))["client_uuid"]
    token, _ = mint_llm_token(client_uuid=client_uuid, email="alice@example.com")
    h = {"Authorization": f"Bearer {token}", **_personne("alice")}
    res = client.post("/tools/v1/mesreunions/chercher", headers=h, json={"arguments": {"q": "x"}})
    assert res.status_code == 200, res.text


# ── Registre ──────────────────────────────────────────────────────────────────

def test_registre_refuse_mcp_sans_auth_et_ecarte_exchange():
    from app.tools.registry import parse_registry
    services = parse_registry(json.dumps({"services": [
        {"id": "video", "protocol": "mcp", "upstream": "https://video.test/mcp", "auth": "none", "tools": {"importer": {}}},
        {"id": "reunions", "protocol": "rest", "upstream": "https://r.test", "identity": "exchange",
         "tools": {"chercher": {"method": "GET", "path": "/s", "parameters": {"type": "object"}}}},
        {"id": "ok", "protocol": "rest", "upstream": "https://ok.test",
         "tools": {"lire": {"method": "GET", "path": "/x", "parameters": {"type": "object"}},
                   "effacer": {"method": "DELETE", "path": "/x", "parameters": {"type": "object"}}}},
    ]}))
    assert [s.id for s in services] == ["ok"]
    assert list(services[0].tools) == ["lire"]  # DELETE refusé
    assert parse_registry("pas du json") == []
