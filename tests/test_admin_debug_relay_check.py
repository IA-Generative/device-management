"""
Contrôle « Relay-assistant » de la page /admin/debug.

Vu en production le 2026-11-08 : le chart ne déploie pas le relais nginx (l'api sert
/relay-assistant/* elle-même) et pose DM_RELAY_ASSISTANT_URL="" dans la ConfigMap. Le
contrôle tombait alors sur urlopen("/healthz") : « unknown url type: '/healthz' », un
voyant rouge pour une absence voulue.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://dev:dev@localhost:5432/bootstrap")
os.environ.setdefault("ADMIN_SESSION_SECRET", "changeme-dev-only")
# Le routeur admin n'est monté qu'en mode admin ou all (app/main.py).
os.environ.setdefault("DM_RUNTIME_MODE", "all")

from app.admin.router import _relay_check_detail


def _urlopen_ok():
    cm = MagicMock()
    cm.__enter__.return_value = cm
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def test_variable_vide_relais_non_deploye():
    urlopen = _urlopen_ok()
    with patch.dict(os.environ, {"DM_RELAY_ASSISTANT_URL": ""}):
        assert _relay_check_detail(urlopen) == "non déployé"
    urlopen.assert_not_called()


def test_variable_blanche_relais_non_deploye():
    urlopen = _urlopen_ok()
    with patch.dict(os.environ, {"DM_RELAY_ASSISTANT_URL": "   "}):
        assert _relay_check_detail(urlopen) == "non déployé"
    urlopen.assert_not_called()


def test_variable_absente_defaut_compose():
    urlopen = _urlopen_ok()
    env = {k: v for k, v in os.environ.items() if k != "DM_RELAY_ASSISTANT_URL"}
    with patch.dict(os.environ, env, clear=True):
        assert _relay_check_detail(urlopen) == "nginx OK"
    urlopen.assert_called_once_with("http://relay-assistant/healthz", timeout=3)


def test_variable_posee_interroge_healthz():
    urlopen = _urlopen_ok()
    with patch.dict(os.environ, {"DM_RELAY_ASSISTANT_URL": "http://dm-relay-assistant:8080/"}):
        assert _relay_check_detail(urlopen) == "nginx OK"
    urlopen.assert_called_once_with("http://dm-relay-assistant:8080/healthz", timeout=3)


# ─── Bandeau d'état des pages admin (/admin/api/debug/status) ─────────────────────────


def _admin_client():
    from fastapi.testclient import TestClient

    from app.admin.auth import _sign_session
    from app.main import app
    client = TestClient(app)
    client.cookies.set("dm_admin_session", _sign_session({
        "sub": "test-sub", "email": "admin@test.com", "name": "Test Admin",
        "exp": int(__import__("time").time()) + 3600,
    }))
    return client


def _conn_ok():
    conn = MagicMock()
    conn.cursor.return_value.execute.return_value = None
    return conn


def test_bandeau_relais_non_deploye_tous_operationnels():
    """Variable vide : le relais n'entre pas dans le bandeau, qui reste vert."""
    urlopen = _urlopen_ok()
    env = {"DM_RELAY_ASSISTANT_URL": "", "DM_AUTH_JWKS_URL": "", "LLM_BASE_URL": ""}
    with patch.dict(os.environ, env), \
         patch("app.admin.router.get_db_connection", return_value=_conn_ok()), \
         patch("urllib.request.urlopen", urlopen):
        resp = _admin_client().get("/admin/api/debug/status")
    assert resp.status_code == 200
    assert "Tous les services sont operationnels" in resp.text
    assert "relay" not in resp.text
    urlopen.assert_not_called()


def test_bandeau_relais_deploye_sonde_healthz():
    urlopen = _urlopen_ok()
    env = {"DM_RELAY_ASSISTANT_URL": "http://dm-relay-assistant", "DM_AUTH_JWKS_URL": "", "LLM_BASE_URL": ""}
    with patch.dict(os.environ, env), \
         patch("app.admin.router.get_db_connection", return_value=_conn_ok()), \
         patch("urllib.request.urlopen", urlopen):
        resp = _admin_client().get("/admin/api/debug/status")
    assert resp.status_code == 200
    assert "Tous les services sont operationnels" in resp.text
    assert "relay" in resp.text
    urlopen.assert_called_once_with("http://dm-relay-assistant/healthz", timeout=3)


def test_relais_injoignable_remonte_l_erreur():
    urlopen = MagicMock(side_effect=OSError("connexion refusée"))
    with patch.dict(os.environ, {"DM_RELAY_ASSISTANT_URL": "http://dm-relay-assistant:8080"}):
        try:
            _relay_check_detail(urlopen)
        except OSError as e:
            assert "refusée" in str(e)
        else:
            raise AssertionError("un relais déployé mais injoignable doit rester une erreur")
