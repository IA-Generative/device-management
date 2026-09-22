"""Feed natif LibreOffice (<update-information>) — GET /catalog/{slug}/update.xml.

Le plugin LibreOffice embarque, cuit au build, une liste d'URL de feed
`<bootstrap>/catalog/mirai-libreoffice/update.xml`. LibreOffice l'interroge
ANONYMEMENT avec sa propre pile HTTP : ni relay-headers, ni UUID client, donc
ni cohorte ni canary ici — le ciblage reste porté par la directive `update`
de /config. Le feed annonce la dernière version `published`.

Les interactions DB sont mockées (même approche que test_enriched_config.py).
"""
from __future__ import annotations

import importlib
import os
import sys
import types
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

NS_UPDATE = "http://openoffice.org/extensions/update/2006"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS = {"u": NS_UPDATE}


def _setup_env() -> None:
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_RELAY_ENABLED"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_RELAY_REQUIRE_KEY_FOR_SECRETS"] = "false"
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"


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


def _make_cursor_mock(cursor_rows_by_query: dict) -> MagicMock:
    """Cursor mock : fetchone/fetchall répondent selon un fragment de la
    dernière requête SQL exécutée ; enregistre (sql, params) dans cur.calls."""
    cur = MagicMock()
    cur.calls = []
    _last_sql: list[str] = [""]

    def _execute(sql, params=None):
        _last_sql[0] = sql
        cur.calls.append((sql, params))

    def _fetchall():
        sql = _last_sql[0]
        for fragment, rows in cursor_rows_by_query.items():
            if fragment in sql:
                return list(rows)
        return []

    def _fetchone():
        rows = _fetchall()
        return rows[0] if rows else None

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _install_db_mock(mod, cursor_rows_by_query: dict):
    """Patch mod.psycopg2.connect → conn mock ; renvoie (patcher, cursor)."""
    cur = _make_cursor_mock(cursor_rows_by_query)
    conn = MagicMock()
    conn.autocommit = True
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.close = MagicMock()
    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    return patcher, cur


PLUGIN_ROW = (7, "libreoffice", "fr.gouv.interieur.mirai")   # id, device_type, extension_id
VERSION_ROW = ("0.0.1.0.32",)


def _get(mod, rows: dict, public_base: str | None = "https://dm.example/bootstrap"):
    patcher, cur = _install_db_mock(mod, rows)
    try:
        if public_base is None:
            os.environ.pop("PUBLIC_BASE_URL", None)
        else:
            os.environ["PUBLIC_BASE_URL"] = public_base
        client = TestClient(mod.app)
        return client.get("/catalog/mirai-libreoffice/update.xml"), cur
    finally:
        patcher.stop()
        os.environ.pop("PUBLIC_BASE_URL", None)


def test_update_xml_announces_latest_published_version():
    mod = _load_module()
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/xml")
    assert res.headers.get("cache-control") == "no-cache"

    root = ET.fromstring(res.content)
    assert root.tag == f"{{{NS_UPDATE}}}description"
    assert root.find("u:identifier", NS).get("value") == "fr.gouv.interieur.mirai"
    assert root.find("u:version", NS).get("value") == "0.0.1.0.32"
    src = root.find("u:update-download/u:src", NS)
    assert src.get(f"{{{NS_XLINK}}}href") == (
        "https://dm.example/bootstrap/catalog/mirai-libreoffice/download/mirai-libreoffice-0.0.1.0.32.oxt"
    )


def test_update_xml_only_queries_published_versions():
    """Les versions expérimentales/taguées ne sont jamais annoncées : la requête
    filtre sur status = 'published' (comme /catalog/{slug}/download sans tag)."""
    mod = _load_module()
    _res, cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    version_sql = [sql for sql, _ in cur.calls if "FROM plugin_versions pv" in sql]
    assert version_sql, "requête versions jamais exécutée"
    assert "pv.status = 'published'" in version_sql[-1]
    assert "tag" not in version_sql[-1]


def test_update_xml_falls_back_to_request_base_url_in_https():
    mod = _load_module()
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [VERSION_ROW],
    }, public_base=None)
    assert res.status_code == 200, res.text
    src = ET.fromstring(res.content).find("u:update-download/u:src", NS)
    href = src.get(f"{{{NS_XLINK}}}href")
    assert href.startswith("https://testserver/"), href   # http → https, sauf localhost
    assert href.endswith("/catalog/mirai-libreoffice/download/mirai-libreoffice-0.0.1.0.32.oxt")


def test_update_xml_404_unknown_slug():
    mod = _load_module()
    res, _cur = _get(mod, {"extension_id FROM plugins": []})
    assert res.status_code == 404


def test_update_xml_404_for_non_libreoffice_plugin():
    mod = _load_module()
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [(8, "firefox", "matisse@interieur.gouv.fr")],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    assert res.status_code == 404


def test_update_xml_404_without_published_version():
    mod = _load_module()
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [],
    })
    assert res.status_code == 404


def test_update_xml_404_when_extension_id_missing():
    """Sans identifiant OXT sur la fiche plugin, LibreOffice ignorerait le feed :
    on répond 404 (et un avertissement en log) plutôt qu'un feed inutilisable."""
    mod = _load_module()
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [(7, "libreoffice", None)],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    assert res.status_code == 404


# ── /update/status : « deferred » n'est pas un échec ─────────────────────
# Le plugin (>= fix/MAJ) rapporte « deferred » au staging ou à l'ouverture du
# dialogue natif, puis « installed » à la réconciliation après redémarrage.
# Compter « deferred » en failed gonflait failure_rate entre les deux phases.

def _status_param(mod, status: str) -> str:
    patcher, cur = _install_db_mock(mod, {})
    try:
        mod._update_campaign_device_status_sync(
            campaign_id=42, client_uuid="uuid-1", status=status,
            version_before="0.0.1.0.31", version_after="0.0.1.0.32", error_detail="",
        )
    finally:
        patcher.stop()
    inserts = [params for sql, params in cur.calls if "INSERT INTO campaign_device_status" in sql]
    assert inserts, "aucun upsert de statut exécuté"
    return inserts[-1][2]


def test_update_status_deferred_maps_to_notified():
    mod = _load_module()
    assert _status_param(mod, "deferred") == "notified"


def test_update_status_installed_and_failures_unchanged():
    mod = _load_module()
    assert _status_param(mod, "installed") == "updated"
    assert _status_param(mod, "failed") == "failed"
    assert _status_param(mod, "checksum_error") == "failed"
    assert _status_param(mod, "download_error") == "failed"
