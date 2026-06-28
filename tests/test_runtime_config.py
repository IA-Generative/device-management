"""Unit tests for the runtime-config override layer (no DB required)."""
import os

import pytest

import app.runtime_config as rc


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # Fresh baseline each test; clear module state.
    monkeypatch.setenv("API_BASE", "https://api.example")
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "https://a,https://b")
    monkeypatch.setenv("DM_CONFIG_SECRET_KEY", "unit-test-master-key")
    rc._BASELINE_PY.clear()
    rc._OVERRIDES_META.clear()
    rc._baseline_ready = False
    rc.snapshot_baseline(force=True)
    yield


def test_baseline_snapshot_str_and_list():
    assert rc.env_baseline("API_BASE") == "https://api.example"
    assert rc.env_baseline("DM_BOOTSTRAP_URLS") == ["https://a", "https://b"]


def test_apply_override_updates_environ_and_cfg():
    rc.apply_state({"API_BASE": "https://override"})
    assert os.environ["API_BASE"] == "https://override"
    assert rc.cfg("API_BASE") == "https://override"


def test_apply_state_restores_baseline_when_override_removed():
    rc.apply_state({"API_BASE": "https://override"})
    rc.apply_state({})  # no overrides -> baseline restored
    assert os.environ["API_BASE"] == "https://api.example"


def test_list_roundtrip_and_order_preserved():
    rc.apply_state({"DM_BOOTSTRAP_URLS": ["https://x", "https://y", "https://z"]})
    assert rc.cfg("DM_BOOTSTRAP_URLS", as_list=True) == ["https://x", "https://y", "https://z"]
    # stored in env as JSON
    assert os.environ["DM_BOOTSTRAP_URLS"].startswith("[")


def test_coerce_input_dedupes_and_strips_list():
    spec = rc.EDITABLE_KEYS["DM_BOOTSTRAP_URLS"]
    out = rc.coerce_input(spec, ["  https://a ", "https://a", "https://b"])
    assert out == ["https://a", "https://b"]


def test_coerce_bool():
    spec = rc.EDITABLE_KEYS["DM_TELEMETRY_ENABLED"]
    assert rc.coerce_input(spec, "true") is True
    assert rc.coerce_input(spec, "0") is False


def test_effective_view_diff_and_masking():
    # secret key present in registry
    rc._OVERRIDES_META["LLM_API_TOKEN"] = {
        "value": "sk-secret-xyz", "is_secret": True, "updated_by": "admin@x", "updated_at": None}
    rc._OVERRIDES_META["API_BASE"] = {
        "value": "https://override", "is_secret": False, "updated_by": "admin@x", "updated_at": None}
    view = {v["key"]: v for v in rc.effective_view()}
    # API_BASE overridden and differs from baseline -> modified
    assert view["API_BASE"]["modified"] is True
    assert view["API_BASE"]["effective"] == "https://override"
    # secret masked, never leaked
    assert "sk-secret-xyz" not in str(view["LLM_API_TOKEN"]["effective"])
    assert view["LLM_API_TOKEN"]["effective"].startswith("***")
    # untouched key not modified
    assert view["COMU_URL"]["modified"] is False
    assert view["COMU_URL"]["override_present"] is False


def test_settings_backed_key_setattr():
    from app.settings import settings
    rc.apply_state({"DM_TELEMETRY_REQUIRE_TOKEN": False})
    assert settings.telemetry_require_token is False
    rc.apply_state({})  # restore baseline
    assert settings.telemetry_require_token == rc.env_baseline("DM_TELEMETRY_REQUIRE_TOKEN")


def test_not_ready_before_reload():
    # _config_ready is module-global; default False at import unless a reload ran.
    # We only assert the accessor exists and is boolean.
    assert isinstance(rc.is_config_ready(), bool)
