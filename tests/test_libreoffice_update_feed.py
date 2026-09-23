"""Feed natif LibreOffice (<update-information>) — GET /catalog/{slug}/update.xml.

Le plugin LibreOffice embarque, cuit au build, une liste d'URL de feed
`<bootstrap>/catalog/mirai-libreoffice/update.xml`. LibreOffice l'interroge
ANONYMEMENT avec sa propre pile HTTP : ni relay-headers, ni UUID client, donc
ni cohorte ni canary ici — le ciblage reste porté par la directive `update`
de /config. Le feed annonce la dernière version `published` servable.

Les interactions DB sont mockées ; environnement et faux psycopg2 sont posés
par la fixture `mod`, qui les défait à la fin de chaque test.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import types
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from app.admin.services import catalog as catalog_svc

NS_UPDATE = "http://openoffice.org/extensions/update/2006"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS = {"u": NS_UPDATE}


_TEST_ENV = {
    "DM_STORE_ENROLL_LOCALLY": "false",
    "DM_STORE_ENROLL_S3": "false",
    "DM_CONFIG_ENABLED": "true",
    "DM_CONFIG_PROFILE": "prod",
    "DM_RELAY_ENABLED": "false",
    "DM_AUTH_VERIFY_ACCESS_TOKEN": "false",
    "DM_TELEMETRY_ENABLED": "true",
    "DM_RELAY_REQUIRE_KEY_FOR_SECRETS": "false",
    "DATABASE_URL": "postgresql://dev:dev@localhost:5432/bootstrap",
}


@pytest.fixture
def mod(monkeypatch):
    """`app.main` rechargé sous un environnement de test et un faux psycopg2.

    Tout passe par ``monkeypatch`` : les variables d'environnement comme
    l'entrée ``sys.modules["psycopg2"]`` sont défaites à la fin du test, là où
    la version précédente les laissait en place pour la suite de la session.
    """
    for key, value in _TEST_ENV.items():
        monkeypatch.setenv(key, value)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    monkeypatch.delitem(sys.modules, "app.settings", raising=False)

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = MagicMock()
    fake_psycopg2.Error = Exception
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    main = importlib.import_module("app.main")
    importlib.reload(main)
    main.psycopg2 = fake_psycopg2
    return main


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
                return list(rows(sql)) if callable(rows) else list(rows)
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
    """Patch mod.psycopg2.connect → conn mock et neutralise le pool (sinon une
    base locale sur :5432 répondrait à la place du mock) ; renvoie (patches, cursor)."""
    cur = _make_cursor_mock(cursor_rows_by_query)
    conn = MagicMock()
    conn.autocommit = True
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.close = MagicMock()
    patches = ExitStack()
    patches.enter_context(patch.object(mod.psycopg2, "connect", return_value=conn))
    patches.enter_context(patch.object(mod, "_pooled_conn", return_value=None))
    return patches, cur


PLUGIN_ROW = (7, "libreoffice", "fr.gouv.interieur.mirai")   # id, device_type, extension_id
VERSION_ROW = ("0.0.1.0.32",)


def _servable(dist_mode, artifact_id, download_url) -> bool:
    """Mêmes conditions que _serve_plugin_download : hors de celles-ci, le
    téléchargement répond 404 quoi qu'annonce le feed."""
    return ((dist_mode == "managed" and artifact_id is not None)
            or (dist_mode in ("download_link", "store") and download_url is not None))


def _version_rows(rows):
    """Faux plugin_versions publiés, du plus récemment publié au plus ancien.
    La servabilité n'est appliquée que si la requête la DEMANDE : un feed qui
    ne filtre pas voit donc aussi les versions dont le binaire est absent."""
    def _query(sql):
        asks_servable = "artifact_id" in sql and "download_url" in sql
        return [(version,) for version, dist_mode, artifact_id, download_url in rows
                if asks_servable is False or _servable(dist_mode, artifact_id, download_url)]
    return _query


# version, distribution_mode, artifact_id, download_url
UNSERVABLE_ROW = ("0.0.1.0.33", "managed", None, None)
SERVABLE_ROW = ("0.0.1.0.32", "managed", 12, None)


def _first_matching_endpoint(mod, path: str) -> str | None:
    """Nom du handler que le routeur atteindrait pour un GET sur `path`."""
    scope = {"type": "http", "method": "GET", "path": path, "root_path": "", "headers": []}
    for route in mod.app.router.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            return route.endpoint.__name__
    return None


def _get(mod, rows: dict, public_base: str | None = "https://dm.example/bootstrap"):
    patches, cur = _install_db_mock(mod, rows)
    try:
        if public_base is None:
            os.environ.pop("PUBLIC_BASE_URL", None)
        else:
            os.environ["PUBLIC_BASE_URL"] = public_base
        client = TestClient(mod.app)
        return client.get("/catalog/mirai-libreoffice/update.xml"), cur
    finally:
        patches.close()
        os.environ.pop("PUBLIC_BASE_URL", None)


def test_update_xml_announces_latest_published_version(mod):
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


def test_update_xml_only_queries_published_versions(mod):
    """Les versions expérimentales/taguées ne sont jamais annoncées : la requête
    filtre sur status = 'published' (comme /catalog/{slug}/download sans tag)."""
    _res, cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    version_sql = [sql for sql, _ in cur.calls if "FROM plugin_versions pv" in sql]
    assert version_sql, "requête versions jamais exécutée"
    assert "pv.status = 'published'" in version_sql[-1]
    assert "tag" not in version_sql[-1]


def test_update_xml_falls_back_to_request_base_url_in_https(mod):
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [VERSION_ROW],
    }, public_base=None)
    assert res.status_code == 200, res.text
    src = ET.fromstring(res.content).find("u:update-download/u:src", NS)
    href = src.get(f"{{{NS_XLINK}}}href")
    assert href.startswith("https://testserver/"), href   # http → https, sauf localhost
    assert href.endswith("/catalog/mirai-libreoffice/download/mirai-libreoffice-0.0.1.0.32.oxt")


def test_update_xml_warns_when_public_base_url_is_empty(mod, caplog):
    """Le repli sur request.base_url reflète l'en-tête Host du client, et
    l'application n'installe pas de TrustedHostMiddleware : on le trace."""
    with caplog.at_level(logging.WARNING, logger="device-management"):
        res, _cur = _get(mod, {
            "extension_id FROM plugins": [PLUGIN_ROW],
            "FROM plugin_versions pv": [VERSION_ROW],
        }, public_base=None)
    assert res.status_code == 200, res.text
    assert any("PUBLIC_BASE_URL" in r.getMessage() for r in caplog.records), caplog.text


def test_update_xml_404_unknown_slug(mod):
    res, _cur = _get(mod, {"extension_id FROM plugins": []})
    assert res.status_code == 404


def test_update_xml_404_for_non_libreoffice_plugin(mod):
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [(8, "firefox", "matisse@interieur.gouv.fr")],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    assert res.status_code == 404


def test_update_xml_404_without_published_version(mod):
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [],
    })
    assert res.status_code == 404


def test_update_xml_404_when_latest_published_is_not_servable(mod):
    """Une version passée en `published` avant l'upload de son artefact (ou dont
    l'artefact a disparu) ne doit PAS être annoncée : chaque poste la tirerait
    en boucle pour un 404 au téléchargement."""
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": _version_rows([UNSERVABLE_ROW]),
    })
    assert res.status_code == 404


def test_update_xml_announces_latest_servable_published_version(mod):
    """Plus récente non servable, précédente servable → c'est la précédente qui
    est annoncée, pas un 404."""
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": _version_rows([UNSERVABLE_ROW, SERVABLE_ROW]),
    })
    assert res.status_code == 200, res.text
    root = ET.fromstring(res.content)
    assert root.find("u:version", NS).get("value") == "0.0.1.0.32"


def test_update_xml_404_when_extension_id_missing(mod):
    """Sans identifiant OXT sur la fiche plugin, LibreOffice ignorerait le feed :
    on répond 404 (et un avertissement en log) plutôt qu'un feed inutilisable."""
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [(7, "libreoffice", None)],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    assert res.status_code == 404


# ── Sûreté : échappement, réversibilité de l'URL, ordre des routes ───────

def test_update_xml_escapes_hostile_attribute_values(mod):
    """L'échappement des attributs est le seul rempart entre une valeur en base et un attribut
    XML : une version ou un identifiant contenant `"`, `&` ou `<` doit ressortir
    intact du re-parsing, pas casser le document ni injecter d'attribut."""
    hostile_id = 'fr.gouv"><script>&x'
    hostile_version = '1.0"&<evil'
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [(7, "libreoffice", hostile_id)],
        "FROM plugin_versions pv": [(hostile_version,)],
    })
    assert res.status_code == 200, res.text
    root = ET.fromstring(res.content)
    assert root.find("u:identifier", NS).get("value") == hostile_id
    assert root.find("u:version", NS).get("value") == hostile_version
    href = root.find("u:update-download/u:src", NS).get(f"{{{NS_XLINK}}}href")
    assert href.endswith(f"/mirai-libreoffice-{hostile_version}.oxt")


def test_announced_filename_reparses_to_the_same_version(mod):
    """Le nom de fichier annoncé doit être celui que /catalog/{slug}/download/
    {filename} sait redécouper : les deux conventions (extension du device_type,
    préfixe `{slug}-`) doivent rester asservies l'une à l'autre."""
    res, _cur = _get(mod, {
        "extension_id FROM plugins": [PLUGIN_ROW],
        "FROM plugin_versions pv": [VERSION_ROW],
    })
    href = ET.fromstring(res.content).find("u:update-download/u:src", NS).get(f"{{{NS_XLINK}}}href")
    filename = href.rsplit("/", 1)[-1]
    assert filename.endswith("." + mod._DEVICE_TYPE_EXT["libreoffice"])

    with patch.object(mod, "_serve_variant_by_filename", return_value=None), \
         patch.object(mod, "_serve_plugin_download") as served:
        mod.catalog_download_file("mirai-libreoffice", filename)
    assert served.call_args.kwargs["version_filter"] == VERSION_ROW[0]


def test_catalog_routes_do_not_shadow_each_other(mod):
    """L'absence d'ombrage tient à l'ordre de déclaration : un futur
    /catalog/{slug}/{quelque_chose} déclaré avant update.xml rendrait le feed
    inatteignable sans qu'aucun test ne le signale."""
    expected = {
        "/catalog/x/updates.xml": "catalog_updates_xml",
        "/catalog/x/update.xml": "catalog_libreoffice_update_xml",
        "/catalog/x": "catalog_detail",
    }
    for path, endpoint in expected.items():
        assert _first_matching_endpoint(mod, path) == endpoint, path


# ── /update/status : « deferred » n'est pas un échec ─────────────────────
# Le plugin (>= fix/MAJ) rapporte « deferred » au staging ou à l'ouverture du
# dialogue natif, puis « installed » à la réconciliation après redémarrage.
# Compter « deferred » en failed gonflait failure_rate entre les deux phases.

def _status_param(mod, status: str) -> str:
    patches, cur = _install_db_mock(mod, {})
    try:
        mod._update_campaign_device_status_sync(
            campaign_id=42, client_uuid="uuid-1", status=status,
            version_before="0.0.1.0.31", version_after="0.0.1.0.32", error_detail="",
        )
    finally:
        patches.close()
    inserts = [params for sql, params in cur.calls if "INSERT INTO campaign_device_status" in sql]
    assert inserts, "aucun upsert de statut exécuté"
    return inserts[-1][2]


def test_update_status_deferred_maps_to_notified(mod):
    assert _status_param(mod, "deferred") == "notified"


def test_update_status_installed_and_failures_unchanged(mod):
    assert _status_param(mod, "installed") == "updated"
    assert _status_param(mod, "failed") == "failed"
    assert _status_param(mod, "checksum_error") == "failed"
    assert _status_param(mod, "download_error") == "failed"


# ── Fiche plugin : extension_id renseignable depuis l'admin ──────────────
# Sans chemin d'écriture, la colonne reste NULL sur un déploiement neuf et le
# feed répond 404 indéfiniment — échec silencieux côté LibreOffice.

def test_update_plugin_accepts_extension_id():
    cur = MagicMock()
    cur.fetchone.return_value = (7,)
    assert catalog_svc.update_plugin(cur, 7, extension_id="fr.gouv.interieur.mirai") is True
    sql, params = cur.execute.call_args[0]
    assert "extension_id = %s" in sql
    assert params[0] == "fr.gouv.interieur.mirai"


def test_update_plugin_still_rejects_unknown_column():
    cur = MagicMock()
    assert catalog_svc.update_plugin(cur, 7, slug="autre") is False
    cur.execute.assert_not_called()
