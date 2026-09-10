"""Firefox auto-update manifest endpoint — /catalog/{slug}/updates.json.

Vérifie que :
- L'endpoint est accessible sans authentification (public)
- Le format de réponse est conforme à la spécification Mozilla
  https://extensionworkshop.com/documentation/manage/updating-your-extension/
- gecko_id (quand renseigné) est utilisé comme clé du manifest
- Le slug est utilisé en fallback quand gecko_id est absent
- update_hash est inclus quand le checksum est disponible
- 404 si le plugin est introuvable ou qu'aucune version n'est publiée
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _setup_env() -> None:
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_RELAY_ENABLED"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DATABASE_URL"] = "postgresql://localhost:5432/bootstrap"


def _load_module():
    _setup_env()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = MagicMock()
    fake_psycopg2.Error = Exception
    sys.modules["psycopg2"] = fake_psycopg2
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    mod.psycopg2 = fake_psycopg2
    return mod


def _make_cursor(plugin_row, version_row):
    """Cursor mock qui renvoie plugin_row puis version_row en séquence."""
    cur = MagicMock()
    calls = []
    results = iter([plugin_row, version_row])

    def _execute(sql, params=None):
        calls.append((sql, params))

    def _fetchone():
        try:
            return next(results)
        except StopIteration:
            return None

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = _fetchone
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _make_conn(cur):
    conn = MagicMock()
    conn.autocommit = True
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_firefox_update_manifest_with_gecko_id():
    """gecko_id est utilisé comme clé du manifest quand il est renseigné."""
    mod = _load_module()
    # plugin row: (id, slug, gecko_id)
    cur = _make_cursor(
        (42, "my-extension", "my-extension@example.com"),
        ("1.2.3", "binaries/firefox/my-extension-1.2.3.xpi", "sha256:" + "a" * 64),
    )
    conn = _make_conn(cur)
    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    try:
        client = TestClient(mod.app, raise_server_exceptions=False)
        res = client.get("/catalog/my-extension/updates.json")
        assert res.status_code == 200, res.text
        data = res.json()
        assert "addons" in data
        assert "my-extension@example.com" in data["addons"]
        updates = data["addons"]["my-extension@example.com"]["updates"]
        assert len(updates) == 1
        assert updates[0]["version"] == "1.2.3"
        assert "update_link" in updates[0]
        assert updates[0]["update_link"].endswith("my-extension-1.2.3.xpi")
        assert updates[0]["update_hash"] == "sha256:" + "a" * 64
    finally:
        patcher.stop()


def test_firefox_update_manifest_without_gecko_id_falls_back_to_slug():
    """Quand gecko_id est NULL/absent, le slug sert de clé."""
    mod = _load_module()
    cur = _make_cursor(
        (7, "another-ext", None),
        ("2.0.0", "binaries/firefox/another-ext-2.0.0.xpi", None),
    )
    conn = _make_conn(cur)
    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    try:
        client = TestClient(mod.app, raise_server_exceptions=False)
        res = client.get("/catalog/another-ext/updates.json")
        assert res.status_code == 200, res.text
        data = res.json()
        assert "another-ext" in data["addons"]
        updates = data["addons"]["another-ext"]["updates"]
        assert updates[0]["version"] == "2.0.0"
        # Pas de checksum → pas de update_hash
        assert "update_hash" not in updates[0]
    finally:
        patcher.stop()


def test_firefox_update_manifest_plugin_not_found():
    """404 si le plugin n'existe pas."""
    mod = _load_module()
    cur = _make_cursor(None, None)
    conn = _make_conn(cur)
    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    try:
        client = TestClient(mod.app, raise_server_exceptions=False)
        res = client.get("/catalog/nonexistent/updates.json")
        assert res.status_code == 404
    finally:
        patcher.stop()


def test_firefox_update_manifest_no_published_version():
    """404 si le plugin existe mais n'a pas de version publiée."""
    mod = _load_module()
    cur = _make_cursor(
        (5, "draft-ext", None),
        None,  # pas de version publiée
    )
    conn = _make_conn(cur)
    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    try:
        client = TestClient(mod.app, raise_server_exceptions=False)
        res = client.get("/catalog/draft-ext/updates.json")
        assert res.status_code == 404
    finally:
        patcher.stop()


def test_firefox_update_manifest_content_type_is_json():
    """L'endpoint doit retourner du JSON (Content-Type: application/json)."""
    mod = _load_module()
    cur = _make_cursor(
        (1, "json-ext", "json-ext@test.com"),
        ("0.9.0", "binaries/firefox/json-ext-0.9.0.xpi", None),
    )
    conn = _make_conn(cur)
    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    try:
        client = TestClient(mod.app, raise_server_exceptions=False)
        res = client.get("/catalog/json-ext/updates.json")
        assert res.status_code == 200
        assert "application/json" in res.headers.get("content-type", "")
    finally:
        patcher.stop()
