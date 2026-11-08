"""
Sonde de la chaîne du relais servi par l'api (app/services/relay_probe.py).

Le voyant doit dire OÙ la chaîne api → proxy de sortie → upstream casse, et une réponse
HTTP de l'upstream, même 401, vaut « la chaîne passe ». Aucun réseau ici : la fabrique
de client est injectée.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services import relay_probe


@pytest.fixture(autouse=True)
def _cache_vide():
    relay_probe.vider_cache()
    yield
    relay_probe.vider_cache()


def _factory_reponse(status: int):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = httpx.Response(status, request=httpx.Request("GET", "https://x"))
    return MagicMock(return_value=client)


def _factory_erreur(exc: Exception):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = exc
    return MagicMock(return_value=client)


# ─── Cibles ────────────────────────────────────────────────────────────────────────────

def test_cibles_configurees_suit_les_variables():
    env = {"RELAY_KEYCLOAK_UPSTREAM": "https://sso.example/realms/mirai/",
           "RELAY_MCR_API_UPSTREAM": "", "RELAY_COMPTE_RENDU_UPSTREAM": "  "}
    with patch.dict(os.environ, env):
        cibles = relay_probe.cibles_configurees()
    assert cibles == {"keycloak": "https://sso.example/realms/mirai/.well-known/openid-configuration"}


def test_aucune_cible_sans_variable():
    env = {k: "" for _, k, _ in relay_probe.CIBLES}
    with patch.dict(os.environ, env):
        assert relay_probe.cibles_configurees() == {}


def test_proxy_sortant_sans_identifiants():
    with patch.dict(os.environ, {"HTTPS_PROXY": "http://user:motdepasse@10.224.32.127:80"}):
        assert relay_probe.proxy_sortant() == "10.224.32.127:80"
    with patch.dict(os.environ, {"HTTPS_PROXY": ""}):
        assert relay_probe.proxy_sortant() is None


# ─── Une sonde ─────────────────────────────────────────────────────────────────────────

def test_http_200_ok_par_proxy():
    with patch.dict(os.environ, {"HTTPS_PROXY": "http://10.224.32.127:80"}):
        r = relay_probe.sonder("keycloak", "https://sso/.well-known/openid-configuration", _factory_reponse(200))
    assert r["status"] == "ok" and r["http"] == 200
    assert "par le proxy 10.224.32.127:80" in r["detail"]


def test_http_401_prouve_que_la_chaine_passe():
    with patch.dict(os.environ, {"HTTPS_PROXY": ""}):
        r = relay_probe.sonder("mcr-api", "https://mcr/api", _factory_reponse(401))
    assert r["status"] == "ok" and r["http"] == 401
    assert "en direct" in r["detail"]


def test_http_5xx_est_une_erreur():
    r = relay_probe.sonder("keycloak", "https://sso", _factory_reponse(502))
    assert r["status"] == "error" and r["http"] == 502
    assert "HTTP 502" in r["detail"]


def test_proxy_error_nomme_le_proxy():
    r = relay_probe.sonder("keycloak", "https://sso", _factory_erreur(httpx.ProxyError("407 Proxy Authentication Required")))
    assert r["status"] == "error" and r["detail"].startswith("proxy : 407")


def test_dns_ou_connexion():
    r = relay_probe.sonder("keycloak", "https://sso", _factory_erreur(httpx.ConnectError("[Errno -2] Name or service not known")))
    assert r["status"] == "error" and r["detail"].startswith("connexion ou DNS")


def test_tls():
    r = relay_probe.sonder("keycloak", "https://sso", _factory_erreur(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")))
    assert r["status"] == "error" and r["detail"].startswith("TLS")


def test_delai_depasse():
    r = relay_probe.sonder("keycloak", "https://sso", _factory_erreur(httpx.ConnectTimeout("timed out")))
    assert r["status"] == "error" and r["detail"].startswith("délai dépassé")


def test_aucune_url_ni_secret_dans_le_detail():
    with patch.dict(os.environ, {"HTTPS_PROXY": "http://u:p@proxy.interne:3128"}):
        r = relay_probe.sonder("keycloak", "https://sso.secret.example/realms/x", _factory_reponse(200))
    assert "sso.secret.example" not in r["detail"]
    assert "u:p" not in r["detail"] and "proxy.interne:3128" in r["detail"]


# ─── Toutes, avec cache ────────────────────────────────────────────────────────────────

def test_sonder_toutes_une_entree_par_cible_configuree():
    env = {"RELAY_KEYCLOAK_UPSTREAM": "https://sso/realms/mirai", "RELAY_MCR_API_UPSTREAM": "https://mcr",
           "RELAY_COMPTE_RENDU_UPSTREAM": "", "HTTPS_PROXY": ""}
    factory = _factory_reponse(200)
    with patch.dict(os.environ, env):
        res = relay_probe.sonder_toutes(client_factory=factory)
    assert set(res["cibles"]) == {"keycloak", "mcr-api"}
    assert res["proxy"] is None
    assert factory.call_count == 2


def test_cache_evite_le_martelement():
    env = {"RELAY_KEYCLOAK_UPSTREAM": "https://sso/realms/mirai", "RELAY_MCR_API_UPSTREAM": "",
           "RELAY_COMPTE_RENDU_UPSTREAM": ""}
    factory = _factory_reponse(200)
    with patch.dict(os.environ, env):
        a = relay_probe.sonder_toutes(client_factory=factory)
        b = relay_probe.sonder_toutes(client_factory=factory)
        c = relay_probe.sonder_toutes(force=True, client_factory=factory)
    assert a is b
    assert c is not a
    assert factory.call_count == 2
