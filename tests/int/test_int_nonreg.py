"""Non-régression d'une instance déployée (INT Scaleway, DGX) — lecture seule.

Doit être VERTE AVANT et APRÈS une montée de version : elle ne vérifie que le
contrat déjà en service (0.9.19), pas les nouveautés (voir
test_int_general_version.py). Aucun jeton, aucun X-Client-UUID : rien n'est
écrit sur l'instance, aucun poste fictif n'apparaît dans le parc.

    DM_BASE_URL=https://bootstrap.example pytest -m int tests/int/test_int_nonreg.py
    DM_BASE_URL=https://onyxia.example/bootstrap pytest -m int tests/int/test_int_nonreg.py

DM_BASE_URL inclut le préfixe éventuel (/bootstrap sur DGX). Plusieurs tirages
par URL (DM_INT_DRAWS, défaut 4) : chaque réplique a son propre cache.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.int]

BASE = os.getenv("DM_BASE_URL", "").rstrip("/")
DRAWS = int(os.getenv("DM_INT_DRAWS", "4"))
MAX_PLUGINS = int(os.getenv("DM_INT_MAX_PLUGINS", "10"))
TIMEOUT = 20
LO_NS = {"u": "http://openoffice.org/extensions/update/2006"}
XLINK = "{http://www.w3.org/1999/xlink}href"

if not BASE:
    pytest.skip("DM_BASE_URL non défini", allow_module_level=True)


def _get(path, **kw):
    return httpx.get(f"{BASE}{path}", timeout=TIMEOUT, follow_redirects=False, **kw)


def _post(path, **kw):
    return httpx.post(f"{BASE}{path}", timeout=TIMEOUT, **kw)


@pytest.fixture(scope="module")
def plugins():
    r = _get("/catalog/api/plugins")
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    assert isinstance(data.get("plugins"), list)
    return data["plugins"][:MAX_PLUGINS]


# ── Santé ────────────────────────────────────────────────────────────────

def test_livez():
    assert _get("/livez").status_code == 200


def test_healthz_shape():
    r = _get("/healthz")
    assert r.status_code in (200, 503)
    assert {"status", "checks"} <= set(r.json())


# ── Catalogue ────────────────────────────────────────────────────────────

def test_catalog_list_contract(plugins):
    for p in plugins:
        assert {"slug", "name", "device_type", "latest_version", "download_url",
                "detail_url"} <= set(p), p.get("slug")
        assert p["download_url"].endswith(f"/catalog/{p['slug']}/download")


def test_catalog_detail_and_page(plugins):
    for p in plugins:
        r = _get(f"/catalog/api/plugins/{p['slug']}")
        assert r.status_code == 200, p["slug"]
        assert r.json()["slug"] == p["slug"]
        page = _get(f"/catalog/{p['slug']}")
        assert page.status_code == 200, p["slug"]


def test_unknown_plugin_is_404():
    assert _get("/catalog/api/plugins/it-nonexistent-plugin-xyz").status_code == 404
    assert _get("/catalog/it-nonexistent-plugin-xyz/download").status_code == 404


def test_download_redirects_to_a_versioned_file_that_is_served(plugins):
    """Sans version : 302 vers /download/<slug>-<version>.<ext>, et ce fichier
    répond — sur chaque tirage (répliques)."""
    for p in plugins:
        for _ in range(DRAWS):
            r = _get(f"/catalog/{p['slug']}/download")
            if r.status_code == 404:
                break                                   # plugin sans version publiée
            assert r.status_code == 302, (p["slug"], r.status_code)
            location = r.headers["location"]
            assert re.search(rf"/catalog/{re.escape(p['slug'])}/download/{re.escape(p['slug'])}-", location), location
            path = location[len(BASE):] if location.startswith(BASE) else location
            with httpx.stream("GET", f"{BASE}{path}" if path.startswith("/") else path,
                              timeout=TIMEOUT, follow_redirects=False) as f:
                assert f.status_code in (200, 302), (location, f.status_code)


# ── Configuration anonyme ────────────────────────────────────────────────

def test_anonymous_config_is_served_without_directive(plugins):
    for p in plugins:
        r = _get(f"/config/{p['slug']}/config.json?profile=int")
        assert r.status_code in (200, 404), (p["slug"], r.status_code)
        if r.status_code == 200:
            body = r.json()
            assert isinstance(body, dict)
            assert not body.get("update"), "aucune directive sans identité de poste"


def test_unknown_device_config_is_not_a_server_error():
    assert _get("/config/it-nonexistent-device-xyz/config.json?profile=int").status_code < 500


# ── Canaux natifs : répondent, jamais en 5xx sauf 503 documenté ─────────

def test_libreoffice_feed_is_well_formed_when_served(plugins):
    for p in [p for p in plugins if p["device_type"] == "libreoffice"]:
        for _ in range(DRAWS):
            r = _get(f"/catalog/{p['slug']}/update.xml")
            assert r.status_code in (200, 404, 503), (p["slug"], r.status_code)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            assert root.find("u:version", LO_NS).get("value")
            href = root.find("u:update-download/u:src", LO_NS).get(XLINK)
            assert href.startswith("https://"), href
            assert f"/catalog/{p['slug']}/download/" in href


# ── Refus sans identité (aucune écriture) ────────────────────────────────

def test_update_status_refuses_without_identity():
    r = _post("/update/status", json={})
    assert 400 <= r.status_code < 500, r.status_code


def test_communication_ack_refuses_without_identity():
    r = _post("/communications/1/ack", json={})
    assert 400 <= r.status_code < 500, r.status_code


def test_enroll_refuses_an_empty_body():
    assert 400 <= _post("/enroll", content=b"").status_code < 500


def test_admin_is_protected():
    r = _get("/admin/")
    assert r.status_code in (301, 302, 303, 307, 401, 403), r.status_code


def test_deploy_api_requires_a_token():
    r = _post("/api/plugins/it-nonexistent-plugin-xyz/deploy")
    assert r.status_code in (401, 403), r.status_code
