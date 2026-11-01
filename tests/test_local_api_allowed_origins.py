"""localApiAllowedOrigins (API locale des extensions) servi dans /config depuis
DM_LOCAL_API_ALLOWED_ORIGINS — CSV, absent quand vide (un "" viderait la valeur locale)."""
import app.runtime_config as rc
from app.main import _apply_overrides


def _base_cfg():
    return {"config": {"foo": "bar"}}


def test_local_api_origins_served_as_csv(monkeypatch):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DM_LOCAL_API_ALLOWED_ORIGINS", "")
    rc.apply_state({"DM_LOCAL_API_ALLOWED_ORIGINS": ["https://portail.example", "http://localhost:8080"]})
    out = _apply_overrides(_base_cfg(), profile="prod", device=None)
    assert out["config"]["localApiAllowedOrigins"] == "https://portail.example,http://localhost:8080"
    rc.apply_state({})  # cleanup


def test_local_api_origins_csv_env_normalised(monkeypatch):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DM_LOCAL_API_ALLOWED_ORIGINS", " https://portail.example , ,https://autre.example ")
    out = _apply_overrides(_base_cfg(), profile="prod", device=None)
    assert out["config"]["localApiAllowedOrigins"] == "https://portail.example,https://autre.example"


def test_local_api_origins_absent_when_empty(monkeypatch):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DM_LOCAL_API_ALLOWED_ORIGINS", "")
    rc.apply_state({})
    out = _apply_overrides(_base_cfg(), profile="prod", device=None)
    assert "localApiAllowedOrigins" not in out["config"]
