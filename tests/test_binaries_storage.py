"""Cloud-native readiness — les binaires ne doivent plus dépendre d'un disque
local (PVC) quand DM_BINARIES_MODE est "presign" ou "proxy" : upload direct
vers S3, et service direct depuis S3 (pas de cache disque, pas de pull-on-miss
vers un pod admin)."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.main as m


@pytest.fixture(autouse=True)
def _restore_binaries_mode():
    original = m.settings.binaries_mode
    yield
    m.settings.binaries_mode = original


def test_pull_binary_from_admin_noop_when_not_local(monkeypatch):
    """No PVC-backed admin pod to pull from outside local mode: must not touch
    the network nor the disk, and must return False."""
    m.settings.binaries_mode = "presign"
    called = {"get": False}

    def _fake_get(*a, **kw):
        called["get"] = True
        raise AssertionError("httpx.get must not be called outside local mode")

    monkeypatch.setattr(m.httpx, "get", _fake_get)
    assert m._pull_binary_from_admin("/data/content/binaries/libreoffice/1.0.0_x.oxt") is False
    assert called["get"] is False


def test_serve_binary_path_presign_redirects_without_touching_disk(monkeypatch, tmp_path):
    m.settings.binaries_mode = "presign"
    m.settings.s3_bucket = "my-bucket"

    fake_client = MagicMock()
    fake_client.generate_presigned_url.return_value = "https://s3.example.com/signed-url"
    monkeypatch.setattr(m, "s3_client", lambda: fake_client)

    # Path deliberately doesn't exist on disk — presign mode must not care.
    missing_path = str(tmp_path / "does" / "not" / "exist.oxt")
    response = m._serve_binary_path(missing_path, "plugin-1.0.0.oxt")

    assert response is not None
    assert response.status_code == 302
    fake_client.generate_presigned_url.assert_called_once()
    _, kwargs = fake_client.generate_presigned_url.call_args
    assert kwargs["Params"]["Key"] == missing_path
    assert kwargs["Params"]["Bucket"] == "my-bucket"


def test_serve_binary_path_local_mode_unchanged(tmp_path):
    m.settings.binaries_mode = "local"
    local_file = tmp_path / "plugin-1.0.0.oxt"
    local_file.write_bytes(b"binary-content")

    response = m._serve_binary_path(str(local_file), "plugin-1.0.0.oxt")
    assert response is not None
    assert response.status_code == 200


def test_persist_plugin_binary_uploads_to_s3_without_local_write(monkeypatch, tmp_path):
    m.settings.binaries_mode = "presign"
    m.settings.s3_bucket = "my-bucket"
    monkeypatch.setattr(m.settings, "local_binaries_dir", str(tmp_path / "unused"))

    fake_client = MagicMock()
    monkeypatch.setattr(m, "s3_client", lambda: fake_client)

    ref = m._persist_plugin_binary(b"payload", "libreoffice/1.0.0_x.oxt", "x.oxt", "libreoffice")

    assert ref == f"{m.S3_BINARIES_PREFIX.rstrip('/')}/libreoffice/1.0.0_x.oxt"
    fake_client.put_object.assert_called_once()
    _, kwargs = fake_client.put_object.call_args
    assert kwargs["Bucket"] == "my-bucket"
    assert kwargs["Key"] == ref
    # No local cache directory was created — no PVC dependency.
    assert not (tmp_path / "unused").exists()


def test_persist_plugin_binary_requires_bucket_when_not_local(monkeypatch):
    m.settings.binaries_mode = "proxy"
    monkeypatch.setattr(m.settings, "s3_bucket", None)
    with pytest.raises(RuntimeError, match="DM_S3_BUCKET"):
        m._persist_plugin_binary(b"payload", "libreoffice/1.0.0_x.oxt", "x.oxt", "libreoffice")


def test_healthz_skips_write_test_when_store_enroll_locally_false(monkeypatch):
    """No PVC-backed enroll_dir to write to: /healthz must not touch the disk
    and must report the local_storage check as "skipped", not failed."""
    monkeypatch.setattr(m.settings, "store_enroll_locally", False)
    # Deliberately unwritable/non-existent — proves the write_test was skipped.
    monkeypatch.setattr(m.settings, "enroll_dir", "/nonexistent/enroll/dir")

    res = TestClient(m.app).get("/healthz")

    assert res.status_code == 200
    checks = res.json()["checks"]
    assert checks["local_storage"] == {"status": "skipped"}
