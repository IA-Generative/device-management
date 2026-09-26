"""Version générale par plugin (issue #40) — câblage et règles, base mockée.

« Publiée » = disponible pour des campagnes ; « générale » = destinée à tout le
parc. Les canaux que le logiciel lit seul (feed LibreOffice, manifestes
Chromium et Gecko, téléchargement sans version) n'annoncent que la générale.
La sémantique SQL (statuts, servabilité, migration) est jugée par un vrai
Postgres dans test_general_version_pg.py ; ici on vérifie que chaque canal
passe bien par le résolveur, et les règles de l'admin.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest
import test_libreoffice_update_feed as _feed_tests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_libreoffice_update_feed import NS, NS_XLINK, PLUGIN_ROW, _install_db_mock

from app.admin.services import catalog as catalog_svc

# Même fixture que les tests du feed : app.main rechargé sous monkeypatch.
mod = _feed_tests.mod

SLUG = "mirai-libreoffice"


def _client_get(mod, path, rows=None, headers=None):
    patches, cur = _install_db_mock(mod, rows or {})
    try:
        return TestClient(mod.app).get(path, headers=headers or {}, follow_redirects=False), cur
    finally:
        patches.close()


# ── Résolveur ────────────────────────────────────────────────────────────

class _Cur:
    """Curseur scripté : chaque execute consomme la réponse suivante."""

    def __init__(self, *answers, fail_first=None):
        self.answers = list(answers)
        self.fail_first = fail_first
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail_first is not None and len(self.calls) == 1:
            raise self.fail_first


    def fetchone(self):
        return self.answers.pop(0) if self.answers else None


class _PgError(Exception):
    def __init__(self, pgcode):
        super().__init__(f"SQLSTATE {pgcode}")
        self.pgcode = pgcode


def test_resolver_without_general_falls_back_to_latest_published(mod):
    cur = _Cur((None,), ("1.4.0",))
    assert mod._general_version(cur, 7) == "1.4.0"
    assert "pv.status = 'published'" in cur.calls[-1][0]


def test_resolver_serves_the_general_even_if_deprecated(mod):
    cur = _Cur((42,), ("1.2.0",))
    assert mod._general_version(cur, 7) == "1.2.0"
    sql, params = cur.calls[-1]
    assert params == (42, 7, ["published", "deprecated"])
    assert "pv.id = %s AND pv.plugin_id = %s" in sql


def test_resolver_never_falls_back_when_the_general_is_not_servable(mod):
    """Générale posée mais retirée ou sans binaire : rien, surtout pas une
    autre version que celle choisie par l'admin."""
    cur = _Cur((42,), None)
    assert mod._general_version(cur, 7) is None
    assert len(cur.calls) == 2


def test_resolver_tolerates_a_schema_not_yet_migrated(mod):
    """Pods 0.9.20 démarrés avant l'application du schéma : repli, pas de 500."""
    cur = _Cur(("1.4.0",), fail_first=_PgError("42703"))   # undefined_column
    assert mod._general_version(cur, 7) == "1.4.0"


def test_resolver_does_not_hide_other_database_errors(mod):
    """Un timeout ne doit pas faire servir la dernière publiée à la place de
    la générale : seule la colonne absente justifie le repli."""
    cur = _Cur(("1.4.0",), fail_first=_PgError("57014"))   # query_canceled
    with pytest.raises(_PgError):
        mod._general_version(cur, 7)


# ── Feed LibreOffice ─────────────────────────────────────────────────────

def test_update_xml_announces_the_general_version(mod):
    with patch.object(mod, "_general_version", return_value="1.2.0") as gv:
        res, _ = _client_get(mod, f"/catalog/{SLUG}/update.xml",
                             {"extension_id FROM plugins": [PLUGIN_ROW]})
    assert res.status_code == 200, res.text
    assert gv.called
    assert ET.fromstring(res.content).find("u:version", NS).get("value") == "1.2.0"


def test_update_xml_version_param_confirms_exactly_that_version(mod):
    with patch.object(mod, "_servable_version", return_value="1.3.0-rc1") as sv, \
         patch.object(mod, "_general_version") as gv:
        res, _ = _client_get(mod, f"/catalog/{SLUG}/update.xml?version=1.3.0-rc1",
                             {"extension_id FROM plugins": [PLUGIN_ROW]})
    assert res.status_code == 200, res.text
    assert not gv.called, "?version= ne doit jamais retomber sur la générale"
    assert sv.call_args.args[2:] == ("1.3.0-rc1", ("published", "experimental", "deprecated"))
    root = ET.fromstring(res.content)
    assert root.find("u:version", NS).get("value") == "1.3.0-rc1"
    href = root.find("u:update-download/u:src", NS).get(f"{{{NS_XLINK}}}href")
    assert href.endswith(f"/catalog/{SLUG}/download/{SLUG}-1.3.0-rc1.oxt")


def test_update_xml_unknown_requested_version_is_404(mod):
    with patch.object(mod, "_servable_version", return_value=None):
        res, _ = _client_get(mod, f"/catalog/{SLUG}/update.xml?version=9.9.9",
                             {"extension_id FROM plugins": [PLUGIN_ROW]})
    assert res.status_code == 404


@pytest.mark.parametrize("bad", ['1.0"><x', "../../etc", "", "a" * 51, "1.0 2"])
def test_update_xml_rejects_a_malformed_version_param(mod, bad):
    res, cur = _client_get(mod, f"/catalog/{SLUG}/update.xml?version={bad}")
    assert res.status_code == 400
    assert cur.calls == [], "rien ne doit partir en base"


def test_update_xml_nothing_to_announce_is_404(mod):
    with patch.object(mod, "_general_version", return_value=None):
        res, _ = _client_get(mod, f"/catalog/{SLUG}/update.xml",
                             {"extension_id FROM plugins": [PLUGIN_ROW]})
    assert res.status_code == 404


# ── Manifestes navigateur ────────────────────────────────────────────────

def test_chrome_catalog_manifest_serves_the_general_version(mod):
    with patch.object(mod, "_general_version", return_value="2.1.0"):
        res, _ = _client_get(mod, "/catalog/iassistant/updates.xml",
                             {"FROM plugins p WHERE p.slug": [(3, "iassistant")]},
                             headers={"Host": "evil.example"})
    assert res.status_code == 200, res.text
    assert "version='2.1.0'" in res.text
    assert "codebase='https://dm.example/bootstrap/catalog/iassistant/download/iassistant-2.1.0.crx'" in res.text
    assert "evil.example" not in res.text


@pytest.mark.parametrize("path", [
    f"/catalog/{SLUG}/update.xml",
    "/catalog/iassistant/updates.xml",
    "/updates/iassistant/scaleway.xml",
    "/updates/matisse/dgx.json",
])
def test_every_native_channel_refuses_without_public_base_url(mod, monkeypatch, path):
    monkeypatch.delenv("PUBLIC_BASE_URL")
    with patch.object(mod, "_general_version", return_value="1.0.0"):
        res, _ = _client_get(mod, path, {
            "extension_id FROM plugins": [PLUGIN_ROW],
            "FROM plugins p WHERE p.slug": [(3, "iassistant")],
        })
    assert res.status_code == 503


def _gecko(mod, checksum):
    rel = {"version": "3.0.0", "filename": "matisse-3.0.0.xpi", "checksum": checksum,
           "extension_id": None, "gecko_id": "matisse@interieur.gouv.fr"}
    with patch.object(mod, "_resolve_update_context", return_value=("matisse", rel)):
        res, _ = _client_get(mod, "/updates/matisse/dgx.json")
    assert res.status_code == 200, res.text
    return res.json()["addons"]["matisse@interieur.gouv.fr"]["updates"][0]


def test_gecko_manifest_carries_update_hash(mod):
    update = _gecko(mod, "sha256:" + "AB" * 32)
    assert update["version"] == "3.0.0"
    assert update["update_hash"] == "sha256:" + "ab" * 32
    assert update["update_link"] == "https://dm.example/bootstrap/catalog/matisse/download/matisse-3.0.0.xpi"


def test_gecko_manifest_omits_an_unusable_hash(mod):
    assert "update_hash" not in _gecko(mod, None)
    assert "update_hash" not in _gecko(mod, "md5:1234")


def test_variant_release_is_restricted_to_the_general_version(mod):
    cur = _Cur(("ext", "gecko"), (42,), ("1.2.0", "gecko/matisse-1.2.0.xpi", "sha256:x"))
    rel = mod._latest_variant_release(cur, 7, "gecko-dgx")
    assert rel["version"] == "1.2.0" and rel["filename"] == "matisse-1.2.0.xpi"
    sql, params = cur.calls[-1]
    assert "pv.id = %s" in sql and params[0] == 42


def test_variant_missing_on_the_general_version_is_none(mod):
    cur = _Cur(("ext", "gecko"), (42,), None)
    assert mod._latest_variant_release(cur, 7, "gecko-dgx") is None


# ── Téléchargement sans version ──────────────────────────────────────────

def test_catalog_download_without_tag_redirects_to_the_general_version(mod):
    with patch.object(mod, "_general_version", return_value="1.2.0"):
        res, _ = _client_get(mod, f"/catalog/{SLUG}/download",
                             {"FROM plugins WHERE slug": [(7, "libreoffice")]})
    assert res.status_code == 302
    assert res.headers["location"] == f"/catalog/{SLUG}/download/{SLUG}-1.2.0.oxt"


# ── Admin : service ──────────────────────────────────────────────────────

def _svc_cur(rows):
    cur = MagicMock()
    cur.fetchone.side_effect = list(rows)
    return cur


def test_set_general_version_records_the_change():
    cur = _svc_cur([(5, "1.0.0", "published"), ("1.1.0", "published", True), (7,)])
    assert catalog_svc.set_general_version(cur, 7, 9) == ("1.0.0", "1.1.0")
    sql, params = cur.execute.call_args[0]
    assert "SET general_version_id = %s" in sql and params == (9, 7)


def test_set_general_version_on_an_unknown_plugin_is_refused():
    cur = _svc_cur([None, None])
    with pytest.raises(catalog_svc.GeneralVersionError, match="Plugin"):
        catalog_svc.set_general_version(cur, 999, None)


@pytest.mark.parametrize("row,message", [
    (None, "introuvable"),
    (("1.1.0", "yanked", True), "yanked"),
    (("1.1.0", "draft", True), "draft"),
    (("1.1.0", "experimental", True), "experimental"),
    (("1.1.0", "published", False), "binaire"),
])
def test_set_general_version_refuses_what_cannot_be_served(row, message):
    cur = _svc_cur([None, row])
    with pytest.raises(catalog_svc.GeneralVersionError, match=message):
        catalog_svc.set_general_version(cur, 7, 9)
    assert not any("SET general_version_id" in c.args[0] for c in cur.execute.call_args_list)


def test_clearing_the_general_version_is_allowed():
    cur = _svc_cur([(5, "1.0.0", "published"), (7,)])
    assert catalog_svc.set_general_version(cur, 7, None) == ("1.0.0", None)


@pytest.mark.parametrize("status", ["yanked", "draft", "experimental"])
def test_the_general_version_cannot_be_withdrawn(status):
    cur = _svc_cur([(1,)])
    with pytest.raises(catalog_svc.GeneralVersionError):
        catalog_svc.update_version_status(cur, 9, status, plugin_id=7)


def test_the_general_version_can_still_be_deprecated():
    cur = _svc_cur([(9,)])
    assert catalog_svc.update_version_status(cur, 9, "deprecated", plugin_id=7) is True
    sql, params = cur.execute.call_args[0]
    assert "AND plugin_id = %s" in sql and params == ("deprecated", 9, 7)


def test_unknown_status_is_refused():
    with pytest.raises(ValueError):
        catalog_svc.update_version_status(MagicMock(), 9, "whatever")


# ── Admin : routes ───────────────────────────────────────────────────────

def _call(handler, *args, **kwargs):
    request = MagicMock()
    request.state.admin_session = {"sub": "t"}
    request.client.host = "127.0.0.1"
    return asyncio.run(handler.__wrapped__(request, *args, **kwargs))


def test_route_sets_the_general_version_and_audits_it():
    from app.admin import router as admin_router
    with patch.object(admin_router, "get_db_connection", return_value=MagicMock()), \
         patch.object(admin_router.catalog_svc, "set_general_version",
                      return_value=("1.0.0", "1.1.0")) as setter, \
         patch.object(admin_router, "audit_log") as audit:
        res = _call(admin_router.catalog_plugin_general_version, 7, version_id="9")
    assert res.status_code == 303
    assert setter.call_args.args[1:] == (7, 9)
    assert audit.call_args.kwargs["action"] == "plugin.general_version"
    assert audit.call_args.kwargs["payload"] == {"from": "1.0.0", "to": "1.1.0"}


def test_route_turns_a_refusal_into_a_400():
    from app.admin import router as admin_router
    with patch.object(admin_router, "get_db_connection", return_value=MagicMock()), \
         patch.object(admin_router.catalog_svc, "set_general_version",
                      side_effect=catalog_svc.GeneralVersionError("non")), \
         patch.object(admin_router, "audit_log") as audit:
        with pytest.raises(HTTPException) as exc:
            _call(admin_router.catalog_plugin_general_version, 7, version_id="9")
    assert exc.value.status_code == 400
    audit.assert_not_called()


def test_purge_spares_the_general_version():
    from app.admin import router as admin_router
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    cur.rowcount = 0
    with patch.object(admin_router, "get_db_connection", return_value=conn), \
         patch.object(admin_router, "audit_log"):
        try:
            _call(admin_router.catalog_versions_purge, 7)
        except Exception:
            pass  # la suite de la purge (fichiers) est hors sujet ici
    deletes = [c.args[0] for c in cur.execute.call_args_list if "DELETE FROM plugin_versions" in c.args[0]]
    assert deletes and all("general_version_id" in sql for sql in deletes)


def test_status_route_is_scoped_to_the_plugin():
    from app.admin import router as admin_router
    with patch.object(admin_router, "get_db_connection", return_value=MagicMock()), \
         patch.object(admin_router.catalog_svc, "update_version_status", return_value=False), \
         patch.object(admin_router, "audit_log") as audit:
        with pytest.raises(HTTPException) as exc:
            _call(admin_router.catalog_version_status, 7, 9, status="deprecated")
    assert exc.value.status_code == 404
    audit.assert_not_called()


def test_route_rejects_a_non_numeric_version_id():
    from app.admin import router as admin_router
    with patch.object(admin_router, "get_db_connection") as conn:
        with pytest.raises(HTTPException) as exc:
            _call(admin_router.catalog_plugin_general_version, 7, version_id="abc")
    assert exc.value.status_code == 400
    conn.assert_not_called()


@pytest.mark.parametrize("status", ["draft", "experimental"])
def test_recreating_the_general_version_with_a_silent_status_is_refused(status):
    """L'upsert de create_version écrasait le statut : contournement de la garde."""
    cur = _svc_cur([(1,)])
    with pytest.raises(catalog_svc.GeneralVersionError):
        catalog_svc.create_version(cur, plugin_id=7, version="1.0.0", status=status)
    assert not any("INSERT INTO plugin_versions" in c.args[0] for c in cur.execute.call_args_list)


# ── Pages publiques du catalogue ─────────────────────────────────────────

def test_catalog_page_shows_the_version_the_download_button_serves(mod):
    """Générale 1.4, 1.5 publiée en canary : la page ne doit pas afficher
    « Télécharger v1.5 » pour un bouton qui sert 1.4."""
    plugin_row = {"id": 7, "slug": SLUG, "name": "MIrAI", "device_type": "libreoffice",
                  "status": "active", "visibility": "public"}
    with patch.object(mod, "_general_version", return_value="1.4.0"):
        patches, cur = _install_db_mock(mod, {
            "SELECT * FROM plugins WHERE slug": [tuple(plugin_row.values())],
            "AND version = %s": [("1.4.0", "Notes 1.4")],
            "COUNT(DISTINCT client_uuid)": [(3,)],
        })
        cur.description = [(k,) for k in plugin_row]
        try:
            res = TestClient(mod.app).get(f"/catalog/{SLUG}")
        finally:
            patches.close()
    assert res.status_code == 200, res.text[:300]
    assert "v1.4.0" in res.text
    assert "v1.5" not in res.text
