"""Complément Word servi par le DM sous `/addins/word/` (dossier `addin/` d'IAssistant-Word),
et client `mirai-word` servi par `/config` et `/enroll` comme les autres plugins.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.llm_utils import load_module, mk_fake_jwt

MANIFESTE = (
    '<OfficeApp><Version>{{VERSION}}</Version><AppDomain>{{KEYCLOAK}}</AppDomain>'
    '<SourceLocation DefaultValue="{{BASE}}/volet.html{{PARAMS}}" /></OfficeApp>'
)


@pytest.fixture
def addin(tmp_path):
    (tmp_path / "manifest.xml").write_text(MANIFESTE, encoding="utf-8")
    (tmp_path / "volet.html").write_text("<!doctype html><title>Volet</title>", encoding="utf-8")
    (tmp_path / "volet.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "volet.js").write_text("export const a = 1;", encoding="utf-8")
    (tmp_path / "icones").mkdir()
    (tmp_path / "icones" / "eloignement-32.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "notes.txt").write_text("hors liste", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(addin, monkeypatch) -> TestClient:
    monkeypatch.setenv("DM_ADDIN_WORD_DIR", str(addin))
    monkeypatch.setenv("DM_ADDIN_WORD_VERSION", "0.1.0.0")
    return TestClient(load_module().app)


def test_manifeste_rendu_pour_le_dm_qui_le_sert(client):
    res = client.get("/addins/word/manifest.xml", params={"profil": "int"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/xml")
    assert res.text == (
        "<OfficeApp><Version>0.1.0.0</Version><AppDomain>https://issuer.from.config.test</AppDomain>"
        '<SourceLocation DefaultValue="https://example.test/bootstrap/addins/word/volet.html?profil=int" />'
        "</OfficeApp>"
    )


def test_manifeste_sans_profil_en_production(client):
    assert "volet.html\"" in client.get("/addins/word/manifest.xml").text
    assert client.get("/addins/word/manifest.xml", params={"profil": "x\" onload=1"}).status_code == 400


@pytest.mark.parametrize("chemin, type_attendu", [
    ("volet.html", "text/html"),
    ("volet.css", "text/css"),
    ("js/volet.js", "text/javascript"),
    ("icones/eloignement-32.png", "image/png"),
])
def test_fichiers_servis_avec_leur_type_et_affichables_par_office(client, chemin, type_attendu):
    res = client.get(f"/addins/word/{chemin}")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith(type_attendu)
    assert res.headers["cache-control"] == "no-cache"
    assert "X-Frame-Options" not in res.headers
    assert "script-src 'self' https://appsforoffice.microsoft.com" in res.headers[
        "content-security-policy-report-only"]


def test_seuls_les_fichiers_du_complement_sont_servis(client):
    assert client.get("/addins/word/notes.txt").status_code == 404
    assert client.get("/addins/word/absent.html").status_code == 404
    assert client.get("/addins/word/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    # Le reste du DM garde son en-tête anti-cadre.
    assert client.get("/healthz").headers["X-Frame-Options"] == "DENY"


def test_sans_repertoire_le_complement_n_est_pas_publie(monkeypatch):
    monkeypatch.delenv("DM_ADDIN_WORD_DIR", raising=False)
    client = TestClient(load_module().app)
    assert client.get("/addins/word/manifest.xml").status_code == 404
    assert client.get("/addins/word/volet.html").status_code == 404


def test_client_mirai_word_configure_et_enrole(tmp_path):
    """Profil `mirai-word` : client Keycloak partagé des plugins à l'anonyme, interrupteur après enrôlement."""
    gabarit = {"configVersion": 1, "config": {
        "enabled": True,
        "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
        "keycloakRealm": "mirai",
        "keycloakClientId": "bootstrap-iassistant",
        "featureToggles": {"eloignement": True},
    }}
    (tmp_path / "mirai-word").mkdir()
    (tmp_path / "mirai-word" / "config.int.json").write_text(json.dumps(gabarit), encoding="utf-8")
    client = TestClient(load_module({"DM_CONFIG_DIR": str(tmp_path)}).app)
    entetes = {"X-Platform-Type": "word", "X-Platform-Version": "16.0", "X-Plugin-Version": "0.1.0",
               "X-Client-UUID": "4b0b9b5c-0000-4000-8000-000000000001"}

    anonyme = client.get("/config/mirai-word/config.json?profile=int", headers=entetes)
    assert anonyme.status_code == 200, anonyme.text
    assert anonyme.json()["config"]["keycloakClientId"] == "bootstrap-iassistant"
    assert anonyme.json()["config"]["keycloakRealm"] == "mirai"

    jeton = mk_fake_jwt({"email": "agent.fictif@exemple.test", "aud": "bootstrap-iassistant"})
    inscrit = client.post(
        "/enroll",
        headers={**entetes, "Authorization": f"Bearer {jeton}"},
        json={"device_name": "mirai-word", "plugin_uuid": entetes["X-Client-UUID"], "email": ""},
    )
    assert inscrit.status_code == 201, inscrit.text
    paire = {"X-Relay-Client": inscrit.json()["relayClientId"], "X-Relay-Key": inscrit.json()["relayClientKey"]}
    assert paire["X-Relay-Client"] and paire["X-Relay-Key"]

    complet = client.get("/config/mirai-word/config.json?profile=int", headers={**entetes, **paire}).json()
    assert complet["features"]["eloignement"] is True
    assert complet["meta"]["device_name"] == "mirai-word"


def test_le_catalogue_telecharge_le_manifeste_du_profil_de_ce_dm(client, monkeypatch):
    monkeypatch.setenv("DM_ADDIN_WORD_PROFILE", "int")
    from app.addins import manifest_download_url

    url = manifest_download_url()
    res = client.get(url)

    assert url == "/addins/word/manifest.xml?profil=int&telecharger=1"
    assert res.status_code == 200
    assert res.headers["content-disposition"] == 'attachment; filename="eloignement-word.xml"'
    assert "volet.html?profil=int" in res.text
    assert "content-disposition" not in client.get("/addins/word/manifest.xml").headers


def test_sans_profil_le_catalogue_donne_le_manifeste_de_production(monkeypatch):
    monkeypatch.delenv("DM_ADDIN_WORD_PROFILE", raising=False)
    from app.addins import manifest_download_url

    assert manifest_download_url() == "/addins/word/manifest.xml?telecharger=1"
