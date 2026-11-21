"""Relais d'outils, protocole `passthrough` : tout ce qui suit le nom du service est propagé
tel quel vers l'amont (verbe, chemin, requête, corps), dans les bornes du registre.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.llm_utils import enroll, load_module, mk_fake_jwt

ELOIGNEMENT = "https://eloignement.test/api"
SSO = "https://sso.test/realms/test/protocol/openid-connect/token"
SECRET_CLIENT = "secret-du-client-de-service"  # nosec B105: valeur factice de test

SERVICE_ELOIGNEMENT = {
    "id": "eloignement", "label": "Éloignement", "protocol": "passthrough", "base_url": ELOIGNEMENT,
    "paths": ["/v1/"], "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
    "max_body_mb": 1, "timeout_s": 20,
    "auth": {"kind": "client_credentials", "token_url": SSO, "client_id": "dm-relais",
             "secret_env": "ELOIGNEMENT_CLIENT_SECRET"},
    "agent_header": "X-Agent-Email",
}
SERVICE_LECTURE = {
    "id": "lecture", "protocol": "passthrough", "base_url": "https://lecture.test",
    "paths": ["/v1/dossiers"], "methods": ["GET"],
    "auth": {"kind": "static", "secret_env": "LECTURE_JETON"},
}
SERVICE_REUNIONS = {
    "id": "mesreunions", "label": "Mes réunions", "protocol": "rest", "upstream": "https://reunions.test",
    "tools": {"chercher": {"method": "GET", "path": "/api/v1/search", "query": ["q"],
                           "parameters": {"type": "object", "required": ["q"],
                                          "properties": {"q": {"type": "string"}}}}},
}
REGISTRE = {"services": [SERVICE_ELOIGNEMENT, SERVICE_LECTURE, SERVICE_REUNIONS]}


class Amont:
    """Faux amonts en mémoire : enregistre les requêtes, répond ce que le test a préparé."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.jetons_emis = 0
        self.reponse = lambda request: httpx.Response(200, json={"ok": True})

    @property
    def appels(self) -> list[httpx.Request]:
        """Les requêtes arrivées chez un amont métier (hors serveur de jetons)."""
        return [r for r in self.requests if r.url.host != "sso.test"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "sso.test":
            self.jetons_emis += 1
            return httpx.Response(200, json={"access_token": f"jeton-de-service-{self.jetons_emis}",
                                             "expires_in": 300, "token_type": "Bearer"})
        if request.url.host == "reunions.test":
            return httpx.Response(200, json={"total": 0, "results": []})
        return self.reponse(request)

    def install(self):
        from app.tools import http_client
        http_client.set_transport_for_tests(httpx.MockTransport(self.handler))


def _setup(registre=REGISTRE, extra_env=None):
    env = {"DM_TOOLS_ENABLED": "true", "DM_TOOLS_REGISTRY": json.dumps(registre), "DM_TOOLS_RATE_PER_MINUTE": "0",
           "ELOIGNEMENT_CLIENT_SECRET": SECRET_CLIENT, "LECTURE_JETON": "jeton-statique"}
    if extra_env:
        env.update(extra_env)
    mod = load_module(env)
    from app.tools import passthrough, throttle
    from app.tools import router as tools_router
    throttle.reset_for_tests()
    tools_router._reset_cache_for_tests()
    passthrough.reset_for_tests()
    amont = Amont()
    amont.install()
    return mod, TestClient(mod.app), amont


def _personne(sub="alice", email="alice@example.com") -> dict[str, str]:
    claims = {"sub": sub, "exp": 4102444800}
    if email:
        claims["email"] = email
    return {"X-User-Authorization": f"Bearer {mk_fake_jwt(claims)}"}


def _entetes(client, **personne) -> dict[str, str]:
    h = enroll(client)
    h.update(_personne(**personne))
    return h


# ── Propagation : verbe, chemin, requête, corps ───────────────────────────────

@pytest.mark.parametrize("verbe", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_chaque_verbe_est_propage_avec_chemin_et_requete(verbe):
    _, client, amont = _setup()
    corps = None if verbe in ("GET", "DELETE") else b'{"depot": "complet"}'
    type_corps = {} if corps is None else {"Content-Type": "application/merge-patch+json"}
    res = client.request(verbe, "/tools/v1/eloignement/v1/dossiers/12?message_id=%3Cabc%40exemple%3E&x=1",
                         headers={**_entetes(client), **type_corps}, content=corps)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}
    sortant = amont.appels[-1]
    assert sortant.method == verbe
    assert sortant.url.host == "eloignement.test"
    assert sortant.url.path == "/api/v1/dossiers/12"
    assert sortant.url.query == b"message_id=%3Cabc%40exemple%3E&x=1"
    assert sortant.content == (corps or b"")
    if corps is not None:
        assert sortant.headers["content-type"] == "application/merge-patch+json"


def test_post_sur_un_seul_segment_est_propage():
    """Le chemin court `<service>/<segment>` est aussi la forme des outils rest/mcp."""
    registre = {"services": [{**SERVICE_ELOIGNEMENT, "paths": ["/"]}]}
    _, client, amont = _setup(registre)
    res = client.post("/tools/v1/eloignement/taches", headers=_entetes(client), json={"a": 1})
    assert res.status_code == 200, res.text
    assert amont.appels[-1].url.path == "/api/taches"
    assert json.loads(amont.appels[-1].content) == {"a": 1}


def test_multipart_transmis_octet_pour_octet():
    _, client, amont = _setup()
    corps = (b"--frontiere\r\nContent-Disposition: form-data; name=\"fichier\"; filename=\"scan.pdf\"\r\n"
             b"Content-Type: application/pdf\r\n\r\n%PDF-1.4\x00\xff\xfe\r\n\x80binaire\r\n"
             b"--frontiere\r\nContent-Disposition: form-data; name=\"metadonnees\"\r\n\r\n"
             b'{"nom": "scan.pdf", "sha256": "ab"}\r\n--frontiere--\r\n')
    type_corps = "multipart/form-data; boundary=frontiere"
    res = client.post("/tools/v1/eloignement/v1/dossiers/12/documents",
                      headers={**_entetes(client), "Content-Type": type_corps}, content=corps)
    assert res.status_code == 200, res.text
    sortant = amont.appels[-1]
    assert sortant.content == corps
    assert sortant.headers["content-type"] == type_corps
    assert sortant.headers["content-length"] == str(len(corps))


# ── Bornes du service ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("chemin", [
    "/tools/v1/eloignement/admin/secrets",          # hors préfixe
    "/tools/v1/eloignement/v1beta/dossiers",        # le préfixe /v1/ s'arrête à un segment
    "/tools/v1/eloignement/v1/%2e%2e/admin",        # remontée encodée
    "/tools/v1/eloignement/v1/dossiers/%252e%252e", # double encodage
    "/tools/v1/eloignement//autre.test/v1/",        # pas de changement d'hôte
    "/tools/v1/inconnu/v1/dossiers",                # service absent du registre
])
def test_chemin_hors_prefixe_refuse_404_sans_appel_amont(chemin):
    _, client, amont = _setup()
    res = client.get(chemin, headers=_entetes(client))
    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "not_found"
    assert amont.requests == []


def test_prefixe_sans_barre_finale_couvre_la_ressource_et_ses_enfants():
    _, client, amont = _setup()
    h = _entetes(client)
    assert client.get("/tools/v1/lecture/v1/dossiers", headers=h).status_code == 200
    assert client.get("/tools/v1/lecture/v1/dossiers/12", headers=h).status_code == 200
    assert client.get("/tools/v1/lecture/v1/dossiers-secrets", headers=h).status_code == 404
    assert [r.url.path for r in amont.appels] == ["/v1/dossiers", "/v1/dossiers/12"]


def test_methode_non_listee_refusee_405_sans_appel_amont():
    _, client, amont = _setup()
    res = client.delete("/tools/v1/lecture/v1/dossiers/12", headers=_entetes(client))
    assert res.status_code == 405, res.text
    assert res.json()["error"]["code"] == "method_not_allowed"
    assert res.headers["Allow"] == "GET"
    assert amont.requests == []


def test_corps_trop_gros_refuse_413_sans_appel_amont():
    _, client, amont = _setup()
    res = client.post("/tools/v1/eloignement/v1/dossiers/12/documents", headers=_entetes(client),
                      content=b"x" * (1024 * 1024 + 1))
    assert res.status_code == 413, res.text
    assert amont.requests == []
    # À la limite exacte, le corps passe.
    res = client.post("/tools/v1/eloignement/v1/dossiers/12/documents", headers=_entetes(client),
                      content=b"x" * (1024 * 1024))
    assert res.status_code == 200, res.text


def test_corps_trop_gros_sans_longueur_annoncee_refuse_413():
    """Un envoi par morceaux (sans Content-Length) est compté à la lecture."""
    _, client, amont = _setup()

    def morceaux():
        for _ in range(5):
            yield b"x" * (256 * 1024)

    res = client.post("/tools/v1/eloignement/v1/dossiers/12/documents", headers=_entetes(client), content=morceaux())
    assert res.status_code == 413, res.text
    assert amont.requests == []


# ── Identité : sans poste ou sans personne, rien ne part ──────────────────────

def test_sans_jeton_utilisateur_401_sans_appel_amont():
    _, client, amont = _setup()
    res = client.post("/tools/v1/eloignement/v1/dossiers", headers=enroll(client), json={})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"
    h = {**enroll(client), "X-User-Authorization": "Bearer pas-un-jeton"}
    assert client.get("/tools/v1/eloignement/v1/dossiers", headers=h).status_code == 401
    assert amont.requests == []


def test_sans_identite_de_poste_401_sans_appel_amont():
    _, client, amont = _setup()
    res = client.get("/tools/v1/eloignement/v1/dossiers", headers=_personne())
    assert res.status_code == 401
    assert amont.requests == []


# ── Les services rest gardent leur comportement ───────────────────────────────

def test_non_regression_service_rest_a_cote_d_un_passthrough():
    _, client, amont = _setup()
    h = _entetes(client)
    res = client.post("/tools/v1/mesreunions/chercher", headers=h, json={"arguments": {"q": "budget"}})
    assert res.status_code == 200, res.text
    assert res.json() == {"result": {"total": 0, "results": []}, "truncated": False}
    assert amont.requests[-1].url.path == "/api/v1/search"
    # Un outil rest n'est pas une porte passthrough : ni autre verbe, ni sous-chemin, ni corps libre.
    assert client.post("/tools/v1/mesreunions/api/v1/search", headers=h, json={}).status_code == 404
    assert client.put("/tools/v1/mesreunions/chercher", headers=h, json={}).status_code == 404
    assert client.post("/tools/v1/mesreunions/chercher", headers=h, content=b"pas du json").status_code == 400
    assert len(amont.requests) == 1


# ── Registre ──────────────────────────────────────────────────────────────────

def test_registre_accepte_la_forme_du_contrat():
    """Entrée posée sous son nom, à la racine du registre (contrat Éloignement, section 2)."""
    from app.tools.registry import parse_registry
    services = parse_registry(json.dumps({
        "eloignement": {"protocol": "passthrough", "base_url": "http://localhost:8000",
                        "paths": ["/v1/"], "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "max_body_mb": 60, "timeout_s": 120,
                        "auth": {"kind": "client_credentials", "token_url": SSO, "client_id": "dm",
                                 "secret_env": "ELOIGNEMENT_CLIENT_SECRET"},
                        "agent_header": "X-Agent-Email"},
        "services": [SERVICE_REUNIONS],
    }))
    assert [s.id for s in services] == ["mesreunions", "eloignement"]
    e = services[1]
    assert (e.protocol, e.upstream, e.paths, e.max_body_mb, e.timeout_s) == (
        "passthrough", "http://localhost:8000", ("/v1/",), 60, 120)
    assert e.methods == ("GET", "POST", "PUT", "PATCH", "DELETE")
    assert e.auth_amont.kind == "client_credentials" and e.agent_header == "X-Agent-Email"


@pytest.mark.parametrize("defaut", [
    {"base_url": "ftp://eloignement.test"},
    {"paths": []},
    {"paths": ["v1/"]},
    {"paths": ["/v1/../"]},
    {"methods": []},
    {"methods": ["GET", "TRACE"]},
    {"auth": None},
    {"auth": {"kind": "none"}},
    {"auth": {"kind": "static", "secret_env": "pas un nom de variable"}},
    {"auth": {"kind": "client_credentials", "secret_env": "X_SECRET", "client_id": "dm"}},
    {"auth": {"kind": "header", "secret_env": "X_SECRET", "header_name": "api key"}},
    {"agent_header": "X-Agent Email"},
])
def test_registre_refuse_un_passthrough_mal_borne(defaut):
    from app.tools.registry import parse_registry
    assert parse_registry(json.dumps({"services": [{**SERVICE_ELOIGNEMENT, **defaut}]})) == []


def test_registre_borne_la_taille_et_le_delai():
    from app.tools.registry import parse_registry
    s = parse_registry(json.dumps({"services": [{**SERVICE_ELOIGNEMENT, "max_body_mb": 5000, "timeout_s": 5000}]}))[0]
    assert s.max_body_mb == 100 and s.timeout_s == 180


# ── En-têtes : ce qui entre, ce qui sort ──────────────────────────────────────

SERVICE_OCR = {
    "id": "ocr", "protocol": "passthrough", "base_url": "https://ocr.test",
    "paths": ["/jobs"], "methods": ["GET", "POST"],
    "auth": {"kind": "header", "header_name": "apikey", "secret_env": "ASYNC_API_KEY_ELOIGNEMENT"},
}


def test_en_tetes_entrants_retires_et_identite_posee_par_le_dm():
    _, client, amont = _setup()
    forges = {
        "X-Agent-Email": "prefet@example.com",   # forgé par le poste : ne passe jamais
        "X_Agent_Email": "prefet@example.com",   # variante à tirets bas, confondue par certains serveurs
        "Cookie": "session=abc",
        "X-Relay-Proxy-Token": "jeton-de-proxy",
        "X-Forwarded-For": "10.0.0.1",
        "Accept-Encoding": "br",
        "Idempotency-Key": "cle-123",
        "If-Match": '"v1"',
        "Accept": "application/json",
    }
    h = {**_entetes(client, email="alice@example.com"), **forges}
    res = client.post("/tools/v1/eloignement/v1/dossiers", headers=h, json={"origine": "courriel"})
    assert res.status_code == 200, res.text
    sortant = amont.appels[-1]
    assert sortant.headers["x-agent-email"] == "alice@example.com"
    assert sortant.headers.get_list("x-agent-email") == ["alice@example.com"]
    assert sortant.headers["authorization"] == "Bearer jeton-de-service-1"
    assert sortant.headers["host"] == "eloignement.test"
    for absent in ("cookie", "x-relay-client", "x-relay-key", "x-relay-proxy-token", "x-user-authorization",
                   "x-forwarded-for", "x-client-id", "x-client-key", "x_agent_email"):
        assert absent not in sortant.headers, absent
    # Aucune trace du jeton de la personne ni de la clé du poste, où que ce soit.
    brut = repr(sortant.headers.raw)
    assert h["X-Relay-Key"] not in brut and h["X-User-Authorization"].split(" ", 1)[1] not in brut
    # Le reste traverse.
    assert sortant.headers["idempotency-key"] == "cle-123"
    assert sortant.headers["if-match"] == '"v1"'
    assert sortant.headers["accept"] == "application/json"
    assert sortant.headers["content-type"] == "application/json"
    assert sortant.headers["accept-encoding"] == "identity"


def test_identite_du_poste_par_llmtoken_n_atteint_pas_l_amont():
    mod, client, amont = _setup()
    from app.llm.tokens import mint_llm_token
    enroll(client)
    client_uuid = next(iter(mod._RELAY_MEMORY_STORE.values()))["client_uuid"]
    llm_token, _ = mint_llm_token(client_uuid=client_uuid, email="poste@example.com")
    res = client.get("/tools/v1/eloignement/v1/dossiers",
                     headers={"Authorization": f"Bearer {llm_token}", **_personne(email="alice@example.com")})
    assert res.status_code == 200, res.text
    sortant = amont.appels[-1]
    assert sortant.headers["authorization"] == "Bearer jeton-de-service-1"
    # L'adresse vient du jeton de la personne, pas de l'enrôlement du poste.
    assert sortant.headers["x-agent-email"] == "alice@example.com"


@pytest.mark.parametrize("email", ["", "pas-une-adresse", "a@b.fr\r\nX-Injecte: 1", "a b@example.com"])
def test_jeton_sans_adresse_exploitable_refuse_403_sans_appel_amont(email):
    _, client, amont = _setup()
    res = client.get("/tools/v1/eloignement/v1/dossiers", headers=_entetes(client, email=email))
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "forbidden"
    assert amont.requests == []


def test_service_sans_agent_header_ne_recoit_pas_l_adresse():
    _, client, amont = _setup()
    h = {**_entetes(client), "X-Agent-Email": "prefet@example.com"}
    assert client.get("/tools/v1/lecture/v1/dossiers", headers=h).status_code == 200
    assert "x-agent-email" not in amont.appels[-1].headers


# ── Authentification amont ────────────────────────────────────────────────────

def test_client_credentials_jeton_demande_puis_garde_en_cache(monkeypatch):
    _, client, amont = _setup()
    from app.tools import passthrough
    horloge = [1000.0]
    monkeypatch.setattr(passthrough, "_maintenant", lambda: horloge[0])
    h = _entetes(client)
    for _ in range(3):
        assert client.get("/tools/v1/eloignement/v1/dossiers", headers=h).status_code == 200
    assert amont.jetons_emis == 1
    demande = [r for r in amont.requests if r.url.host == "sso.test"][0]
    assert demande.method == "POST" and str(demande.url) == SSO
    formulaire = dict(httpx.QueryParams(demande.content.decode()))
    assert formulaire == {"grant_type": "client_credentials", "client_id": "dm-relais",
                          "client_secret": SECRET_CLIENT}
    assert all(r.headers["authorization"] == "Bearer jeton-de-service-1" for r in amont.appels)
    # Peu avant l'expiration (300 s annoncées), le jeton est redemandé.
    horloge[0] += 250
    client.get("/tools/v1/eloignement/v1/dossiers", headers=h)
    assert amont.jetons_emis == 1
    horloge[0] += 30
    client.get("/tools/v1/eloignement/v1/dossiers", headers=h)
    assert amont.jetons_emis == 2
    assert amont.appels[-1].headers["authorization"] == "Bearer jeton-de-service-2"


def test_client_credentials_jeton_refuse_par_l_amont_est_oublie():
    _, client, amont = _setup()
    h = _entetes(client)
    amont.reponse = lambda request: httpx.Response(401, json={"detail": "jeton expiré"})
    assert client.get("/tools/v1/eloignement/v1/dossiers", headers=h).status_code == 401
    amont.reponse = lambda request: httpx.Response(200, json={"ok": True})
    assert client.get("/tools/v1/eloignement/v1/dossiers", headers=h).status_code == 200
    assert amont.jetons_emis == 2


def test_serveur_de_jetons_en_echec_502_sans_appel_amont_ni_fuite():
    _, client, amont = _setup()
    reponses = iter([httpx.Response(401, json={"error": "invalid_client", "error_description": SECRET_CLIENT}),
                     httpx.Response(200, json={"pas": "de jeton"})])
    original = amont.handler

    def handler(request):
        if request.url.host == "sso.test":
            amont.requests.append(request)
            return next(reponses)
        return original(request)

    from app.tools import http_client
    http_client.set_transport_for_tests(httpx.MockTransport(handler))
    for _ in range(2):
        res = client.get("/tools/v1/eloignement/v1/dossiers", headers=_entetes(client))
        assert res.status_code == 502, res.text
        assert res.json()["error"]["code"] == "unavailable"
        assert SECRET_CLIENT not in res.text and "sso.test" not in res.text
    assert amont.appels == []


def test_auth_static_pose_le_jeton_de_la_variable():
    _, client, amont = _setup()
    assert client.get("/tools/v1/lecture/v1/dossiers", headers=_entetes(client)).status_code == 200
    assert amont.appels[-1].headers["authorization"] == "Bearer jeton-statique"
    assert amont.jetons_emis == 0


def test_auth_header_pose_la_cle_dans_l_en_tete_nomme():
    _, client, amont = _setup({"services": [SERVICE_OCR]}, {"ASYNC_API_KEY_ELOIGNEMENT": "cle-ocr"})
    h = {**_entetes(client), "apikey": "cle-forgee-par-le-poste"}
    res = client.post("/tools/v1/ocr/jobs", headers=h, json={"modele": "x"})
    assert res.status_code == 200, res.text
    sortant = amont.appels[-1]
    assert sortant.headers.get_list("apikey") == ["cle-ocr"]
    assert "authorization" not in sortant.headers
    assert "x-agent-email" not in sortant.headers


def test_secret_absent_de_l_environnement_503_sans_appel_amont(monkeypatch):
    _, client, amont = _setup()
    monkeypatch.delenv("LECTURE_JETON")
    res = client.get("/tools/v1/lecture/v1/dossiers", headers=_entetes(client))
    assert res.status_code == 503, res.text
    assert "LECTURE_JETON" not in res.text
    assert amont.requests == []


# ── Réponse amont rendue telle quelle ─────────────────────────────────────────

def test_reponse_amont_rendue_avec_code_corps_et_en_tetes():
    _, client, amont = _setup()
    amont.reponse = lambda request: httpx.Response(
        201, content=b'{"id": 1987, "statut": "brouillon"}',
        headers={"Content-Type": "application/json; charset=utf-8", "Location": "/v1/dossiers/1987",
                 "ETag": '"abc"', "Retry-After": "7", "Set-Cookie": "session=amont", "Server": "amont/1.0",
                 "X-Interne": "detail"})
    res = client.post("/tools/v1/eloignement/v1/dossiers", headers=_entetes(client), json={"origine": "courriel"})
    assert res.status_code == 201
    assert res.content == b'{"id": 1987, "statut": "brouillon"}'
    assert res.headers["content-type"] == "application/json; charset=utf-8"
    assert res.headers["location"] == "/v1/dossiers/1987"
    assert res.headers["etag"] == '"abc"'
    assert res.headers["retry-after"] == "7"
    # Le reste des en-têtes amont ne sort pas.
    for absent in ("set-cookie", "server", "x-interne"):
        assert absent not in res.headers, absent


def test_erreur_metier_amont_rendue_telle_quelle():
    _, client, amont = _setup()
    probleme = {"type": "about:blank", "title": "Conflit", "status": 409, "detail": "Le dossier n'est plus un brouillon."}
    amont.reponse = lambda request: httpx.Response(409, json=probleme,
                                                   headers={"Content-Type": "application/problem+json"})
    res = client.delete("/tools/v1/eloignement/v1/dossiers/12", headers=_entetes(client))
    assert res.status_code == 409
    assert res.headers["content-type"] == "application/problem+json"
    assert res.json() == probleme


def test_reponse_sans_corps_et_redirection_non_suivie():
    _, client, amont = _setup()
    amont.reponse = lambda request: httpx.Response(204)
    res = client.delete("/tools/v1/eloignement/v1/dossiers/12", headers=_entetes(client))
    assert res.status_code == 204 and res.content == b""
    amont.reponse = lambda request: httpx.Response(302, headers={"Location": "https://ailleurs.test/login"})
    res = client.get("/tools/v1/eloignement/v1/dossiers/12", headers=_entetes(client), follow_redirects=False)
    assert res.status_code == 302
    assert [r.url.host for r in amont.appels] == ["eloignement.test", "eloignement.test"]


def test_reponse_binaire_rendue_octet_pour_octet():
    _, client, amont = _setup()
    octets = bytes(range(256)) * 64
    amont.reponse = lambda request: httpx.Response(200, content=octets, headers={"Content-Type": "application/pdf"})
    res = client.get("/tools/v1/eloignement/v1/dossiers/12/documents/55", headers=_entetes(client))
    assert res.status_code == 200 and res.content == octets
    assert res.headers["content-type"] == "application/pdf"


def test_reponse_amont_au_dela_de_la_borne_502():
    _, client, amont = _setup()
    amont.reponse = lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
    res = client.get("/tools/v1/eloignement/v1/dossiers", headers=_entetes(client))
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "unavailable"


# ── Pannes réseau de l'amont ──────────────────────────────────────────────────

def _lever(exc):
    def reponse(request):
        raise exc("connexion refusée par 10.1.2.3:8443 (eloignement.interne)", request=request)
    return reponse


@pytest.mark.parametrize("panne, statut", [
    (httpx.ConnectError, 502), (httpx.ReadError, 502), (httpx.RemoteProtocolError, 502),
    (httpx.ConnectTimeout, 504), (httpx.ReadTimeout, 504),
])
def test_panne_reseau_amont_502_ou_504_sans_detail_interne(panne, statut):
    _, client, amont = _setup()
    amont.reponse = _lever(panne)
    res = client.get("/tools/v1/eloignement/v1/dossiers", headers=_entetes(client))
    assert res.status_code == statut, res.text
    assert res.json()["error"]["code"] == "unavailable"
    for interne in ("10.1.2.3", "eloignement.interne", "eloignement.test", "Traceback", panne.__name__):
        assert interne not in res.text, interne


def test_serveur_de_jetons_muet_504():
    _, client, amont = _setup()

    def handler(request):
        raise httpx.ReadTimeout("délai dépassé", request=request)

    from app.tools import http_client
    http_client.set_transport_for_tests(httpx.MockTransport(handler))
    res = client.get("/tools/v1/eloignement/v1/dossiers", headers=_entetes(client))
    assert res.status_code == 504
    assert "sso.test" not in res.text


# ── Journal ───────────────────────────────────────────────────────────────────

def test_journal_passthrough_sans_requete_ni_corps_ni_adresse(caplog):
    import logging
    _, client, _ = _setup()
    with caplog.at_level(logging.INFO, logger="dm-tools-audit"):
        client.post("/tools/v1/eloignement/v1/dossiers?message_id=identifiant-du-courriel",
                    headers=_entetes(client, email="alice@example.com"), json={"commentaire": "texte-de-l-agent"})
    lignes = [r.getMessage() for r in caplog.records if r.name == "dm-tools-audit"]
    entree = json.loads(lignes[-1])
    assert (entree["service"], entree["tool"], entree["status"], entree["acts"]) == (
        "eloignement", "POST /v1/dossiers", 200, True)
    for absent in ("identifiant-du-courriel", "texte-de-l-agent", "alice@example.com", "jeton-de-service"):
        assert absent not in lignes[-1], absent
