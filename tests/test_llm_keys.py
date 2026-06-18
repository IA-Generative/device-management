import json
import os
import sys
from urllib import request as urllib_request

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root not in sys.path:
    sys.path.insert(0, root)

from app.services import litellm
from app.services.crypto import encrypt_secret, decrypt_secret


def test_encrypt_decrypt_round_trip():
    pepper = "unit-test-pepper"
    token = encrypt_secret("sk-device-123", pepper)
    assert token != "sk-device-123"
    assert decrypt_secret(token, pepper) == "sk-device-123"


def test_decrypt_with_wrong_pepper_returns_none():
    token = encrypt_secret("sk-device-123", "pepper-a")
    assert decrypt_secret(token, "pepper-b") is None


def test_decrypt_garbage_returns_none():
    assert decrypt_secret("not-a-token", "pepper") is None


def test_resolve_admin_base_url_prefers_explicit():
    assert litellm.resolve_admin_base_url("https://admin.example/", "https://x/v1") == "https://admin.example"


def test_resolve_admin_base_url_strips_v1_from_llm_base():
    assert litellm.resolve_admin_base_url("", "https://litellm.example/v1") == "https://litellm.example"
    assert litellm.resolve_admin_base_url("", "https://litellm.example/v1/") == "https://litellm.example"


def test_resolve_admin_base_url_keeps_non_v1_path():
    assert litellm.resolve_admin_base_url("", "https://litellm.example") == "https://litellm.example"


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_generate_device_key_builds_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(json.dumps({"key": "sk-new", "expires": "2030-01-01"}).encode())

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)

    result = litellm.generate_device_key(
        admin_base_url="https://litellm.example",
        admin_key="sk-admin",
        key_alias="dm-abc",
        duration_seconds=3600,
        metadata={"client_uuid": "abc"},
    )

    assert result["key"] == "sk-new"
    assert captured["url"] == "https://litellm.example/key/generate"
    # urllib title-cases header names
    assert captured["headers"].get("Authorization") == "Bearer sk-admin"
    assert captured["body"]["key_alias"] == "dm-abc"
    assert captured["body"]["duration"] == "3600s"
    assert captured["body"]["metadata"] == {"client_uuid": "abc"}


class _FakeCursor:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params


def test_revoke_device_llm_key_deletes_on_hub_and_marks_db(monkeypatch):
    from app.admin.services import devices

    calls = {}
    monkeypatch.setattr(devices.settings, "llm_admin_key", "sk-admin")
    monkeypatch.setattr(devices.settings, "llm_admin_base_url", "https://hub.example")
    monkeypatch.setattr(devices.settings, "llm_base_url", "")
    monkeypatch.setattr(devices._litellm, "delete_device_key", lambda **kw: calls.update(kw))

    cur = _FakeCursor()
    devices.revoke_device_llm_key(cur, "abc-uuid")

    assert calls["key_alias"] == "dm-abc-uuid"
    assert calls["admin_base_url"] == "https://hub.example"
    assert "device_llm_keys" in cur.sql
    assert cur.params == ("abc-uuid",)


def test_revoke_device_llm_key_skips_hub_when_unconfigured(monkeypatch):
    from app.admin.services import devices

    called = {"hit": False}
    monkeypatch.setattr(devices.settings, "llm_admin_key", "")
    monkeypatch.setattr(devices.settings, "llm_admin_base_url", "")
    monkeypatch.setattr(devices.settings, "llm_base_url", "")

    def _boom(**kw):
        called["hit"] = True

    monkeypatch.setattr(devices._litellm, "delete_device_key", _boom)

    cur = _FakeCursor()
    devices.revoke_device_llm_key(cur, "abc-uuid")

    assert called["hit"] is False
    assert "device_llm_keys" in cur.sql
