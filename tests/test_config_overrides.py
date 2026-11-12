"""DM_CONFIG_OVERRIDES : clés de config.json imposées par le déploiement (values du chart),
fusionnées sur le gabarit, sous les clés calculées par le DM et sous la surcharge catalogue."""
import json

import app.runtime_config as rc
from app.main import _apply_overrides
from app.settings import settings


def _base_cfg():
    return {"config": {"foo": "bar", "mesReunionsBaseUrl": "https://prod.example"}}


def _setup(monkeypatch, raw: str):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DM_CONFIG_OVERRIDES", raw)


def test_cle_imposee_par_le_deploiement(monkeypatch):
    _setup(monkeypatch, json.dumps({"mesReunionsBaseUrl": "https://int.example", "nouvelle": [1, 2]}))
    out = _apply_overrides(_base_cfg(), profile="prod", device=None)
    assert out["config"]["mesReunionsBaseUrl"] == "https://int.example"
    assert out["config"]["nouvelle"] == [1, 2]
    assert out["config"]["foo"] == "bar"


def test_vide_ou_invalide_sans_effet(monkeypatch):
    for raw in ("", "   ", "{pas du json", "[1, 2]"):
        _setup(monkeypatch, raw)
        out = _apply_overrides(_base_cfg(), profile="prod", device=None)
        assert out["config"]["mesReunionsBaseUrl"] == "https://prod.example"


def test_cles_reservees_ignorees(monkeypatch):
    _setup(monkeypatch, json.dumps({"links": [{"id": "x", "url": "https://x"}],
                                    "telemetryEnabled": True, "toolsEndpoint": "https://pirate"}))
    out = _apply_overrides(_base_cfg(), profile="prod", device=None)
    assert out["config"]["telemetryEnabled"] is bool(settings.telemetry_enabled)
    assert out["config"]["links"] == []
    assert "toolsEndpoint" not in out["config"]


def test_hot_reload_depuis_l_admin(monkeypatch):
    _setup(monkeypatch, "")
    rc.apply_state({"DM_CONFIG_OVERRIDES": json.dumps({"mesReunionsBaseUrl": "https://admin.example"})})
    try:
        out = _apply_overrides(_base_cfg(), profile="prod", device=None)
        assert out["config"]["mesReunionsBaseUrl"] == "https://admin.example"
    finally:
        rc.apply_state({})
