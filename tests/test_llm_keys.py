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
