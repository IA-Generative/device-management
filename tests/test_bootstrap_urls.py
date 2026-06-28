"""Stage 8: ordered bootstrap_urls surfaced in the /api/config response."""
import app.runtime_config as rc
from app.main import _apply_overrides


def _base_cfg():
    return {"config": {"foo": "bar"}}


def test_bootstrap_urls_ordered_in_response(monkeypatch):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    # Override-aware: cfg reads os.environ, which apply_state keeps current.
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "")
    rc.apply_state({"DM_BOOTSTRAP_URLS": ["https://dm-a", "https://dm-b", "https://dm-c"]})
    out = _apply_overrides(_base_cfg(), profile="default", device=None)
    assert out["config"]["bootstrapUrls"] == ["https://dm-a", "https://dm-b", "https://dm-c"]
    rc.apply_state({})  # cleanup


def test_bootstrap_urls_absent_when_empty(monkeypatch):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "")
    rc.apply_state({})
    out = _apply_overrides(_base_cfg(), profile="default", device=None)
    assert "bootstrapUrls" not in out["config"]
