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


def test_relais_injoignable_remonte_l_erreur():
    urlopen = MagicMock(side_effect=OSError("connexion refusée"))
    with patch.dict(os.environ, {"DM_RELAY_ASSISTANT_URL": "http://dm-relay-assistant:8080"}):
        try:
            _relay_check_detail(urlopen)
        except OSError as e:
            assert "refusée" in str(e)
        else:
            raise AssertionError("un relais déployé mais injoignable doit rester une erreur")
