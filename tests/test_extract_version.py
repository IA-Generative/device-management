"""extract-version (wizard admin) : fallback sur dm-manifest.json.

dm-manifest.json est la source primaire (changelog[0].version, device_type) ;
manifest.json WebExtension et les heuristiques par extension ne servent que de
fallback. Un XPI Thunderbird legacy (TB60 : install.rdf + bootstrap.js) n'a pas
de manifest.json et doit etre detecte correctement.
"""

import io
import json
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.auth import SESSION_COOKIE, _sign_session
from app.admin.router import router as admin_router

DM_MANIFEST_TB60 = {
    "slug": "matisse",
    "device_type": "matisse",
    "changelog": [
        {"version": "0.16.1", "date": "2026-08-27", "changes": ["fix a", "fix b"]},
        {"version": "0.16.0", "date": "2026-08-20", "changes": ["feat"]},
    ],
}


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, _sign_session({
        "sub": "t", "email": "admin@test.com", "name": "T", "exp": int(time.time()) + 3600,
    }))
    return c


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _extract(client, data: bytes, filename: str) -> dict:
    r = client.post("/admin/api/deploy/extract-version",
                    files={"binary": (filename, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_legacy_xpi_version_and_type_from_dm_manifest(client):
    data = _zip_bytes({
        "install.rdf": "<RDF/>",
        "bootstrap.js": "",
        "dm-manifest.json": json.dumps(DM_MANIFEST_TB60),
    })
    out = _extract(client, data, "mirai-assistant-tb60.xpi")
    assert out["version"] == "0.16.1"
    assert out["source"] == "package"
    assert out["device_type"] == "matisse"
    assert out["release_notes"] == "- fix a\n- fix b"


def test_dm_manifest_wins_over_manifest_json(client):
    data = _zip_bytes({
        "manifest.json": json.dumps({"version": "2.0.0", "browser_specific_settings": {"gecko": {}}}),
        "dm-manifest.json": json.dumps(DM_MANIFEST_TB60),
    })
    out = _extract(client, data, "plugin.xpi")
    assert out["version"] == "0.16.1"
    assert out["device_type"] == "matisse"


def test_manifest_json_is_fallback_when_dm_manifest_incomplete(client):
    dm_manifest = {"slug": "matisse"}  # no changelog, no device_type
    data = _zip_bytes({
        "manifest.json": json.dumps({"version": "2.0.0", "browser_specific_settings": {"thunderbird": {"strict_min_version": "128.0"}}}),
        "dm-manifest.json": json.dumps(dm_manifest),
    })
    out = _extract(client, data, "plugin.xpi")
    assert out["version"] == "2.0.0"
    assert out["source"] == "package"
    assert out["device_type"] == "matisse"


def test_dm_manifest_thunderbird_alias_maps_to_matisse(client):
    manifest = dict(DM_MANIFEST_TB60, device_type="thunderbird")
    data = _zip_bytes({"install.rdf": "<RDF/>", "dm-manifest.json": json.dumps(manifest)})
    out = _extract(client, data, "plugin.xpi")
    assert out["device_type"] == "matisse"


def test_dm_manifest_unknown_device_type_is_ignored(client):
    manifest = dict(DM_MANIFEST_TB60, device_type="toaster")
    data = _zip_bytes({"install.rdf": "<RDF/>", "dm-manifest.json": json.dumps(manifest)})
    out = _extract(client, data, "plugin.xpi")
    assert out["device_type"] == "firefox"


def test_legacy_xpi_without_dm_manifest_unchanged(client):
    data = _zip_bytes({"install.rdf": "<RDF/>", "bootstrap.js": ""})
    out = _extract(client, data, "plugin-0.9.0.xpi")
    assert out["version"] == "0.9.0"
    assert out["source"] == "filename"
    assert out["device_type"] == "firefox"


def test_invalid_dm_manifest_does_not_break_detection(client):
    data = _zip_bytes({"install.rdf": "<RDF/>", "dm-manifest.json": "{not json"})
    out = _extract(client, data, "plugin-0.9.0.xpi")
    assert out["version"] == "0.9.0"
    assert out["device_type"] == "firefox"
