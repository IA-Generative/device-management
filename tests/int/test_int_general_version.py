"""Recette de la version générale (issue #40) sur une instance déployée.

À jouer dans un NAMESPACE DÉDIÉ (ex. dm-feeds-test), jamais sur `bootstrap` :
la suite publie des versions et crée une campagne pour le plugin de test.

Prérequis (une fois, dans l'admin de l'instance dédiée) : un plugin
`libreoffice` de slug DM_TEST_PLUGIN_SLUG, avec son identifiant OXT renseigné.

    DM_BASE_URL=https://dm-feeds-test.example \\
    DM_ADMIN_TOKEN=<DM_QUEUE_ADMIN_TOKEN de l'instance> \\
    DM_TEST_PLUGIN_SLUG=it-feed-plugin \\
    pytest -m int tests/int/test_int_general_version.py

Scénario : v1 publiée et générale ; v2 publiée SANS être générale (canary) →
tous les canaux restent sur v1 ; directive ciblée → lien épinglé v2 dont le
binaire a l'empreinte annoncée ; update.xml?version= ; v2 rendue générale →
canaux sur v2 ; retour de la générale sur v1. Numéros de version horodatés :
la suite est rejouable.
"""
from __future__ import annotations

import hashlib
import io
import os
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.int]

BASE = os.getenv("DM_BASE_URL", "").rstrip("/")
TOKEN = os.getenv("DM_ADMIN_TOKEN", "")
SLUG = os.getenv("DM_TEST_PLUGIN_SLUG", "")
PROFILE = os.getenv("DM_TEST_PROFILE", "int")
TIMEOUT = 30
LO_NS = {"u": "http://openoffice.org/extensions/update/2006"}
XLINK = "{http://www.w3.org/1999/xlink}href"

if not (BASE and TOKEN and SLUG):
    pytest.skip("DM_BASE_URL, DM_ADMIN_TOKEN et DM_TEST_PLUGIN_SLUG requis", allow_module_level=True)

_STAMP = int(time.time())
V1 = f"9.{_STAMP}.1"
V2 = f"9.{_STAMP}.2"


def _oxt(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("description.xml", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<description xmlns="http://openoffice.org/extensions/description/2006">\n'
            f'  <identifier value="it.general.version"/>\n  <version value="{version}"/>\n'
            "</description>\n"))
        zf.writestr("README.txt", f"recette #40 {version}")
    return buf.getvalue()


def _get(path, **kw):
    return httpx.get(f"{BASE}{path}", timeout=TIMEOUT, follow_redirects=False, **kw)


def _auth():
    return {"X-Admin-Token": TOKEN}


def _feed_version(query=""):
    r = _get(f"/catalog/{SLUG}/update.xml{query}")
    if r.status_code != 200:
        return r.status_code
    return ET.fromstring(r.content).find("u:version", LO_NS).get("value")


def _general():
    return _get(f"/catalog/api/plugins/{SLUG}").json().get("general_version")


def _download_version():
    r = _get(f"/catalog/{SLUG}/download")
    assert r.status_code == 302
    return r.headers["location"].rsplit(f"{SLUG}-", 1)[1].rsplit(".", 1)[0]


@pytest.fixture(scope="module")
def plugin_id():
    r = _get(f"/catalog/api/plugins/{SLUG}")
    assert r.status_code == 200, f"plugin de test {SLUG} absent : le créer dans l'admin"
    body = r.json()
    assert body["device_type"] == "libreoffice"
    return body["id"]


@pytest.fixture(scope="module")
def published(plugin_id):
    """v1 publiée ET générale, puis v2 publiée seulement (canary). Renvoie
    {version: version_id}. Publier v2 déprécie v1 : c'est le cas réel, la
    générale dépréciée doit rester servie."""
    campaigns, ids = [], {}
    for version, general in ((V1, True), (V2, False)):
        r = httpx.post(f"{BASE}/api/plugins/{SLUG}/deploy", headers=_auth(), timeout=TIMEOUT,
                       files={"binary": (f"{SLUG}-{version}.oxt", _oxt(version))},
                       data={"version": version, "strategy": "immediate",
                             "general": "true" if general else ""})
        assert r.status_code == 201, r.text[:300]
        assert r.json()["general"] is general
        ids[version] = r.json()["version_id"]
        if r.json().get("campaign_id"):
            campaigns.append(r.json()["campaign_id"])
    yield ids
    for cid in campaigns:                               # nettoyage : aucune campagne active laissée
        httpx.patch(f"{BASE}/api/campaigns/{cid}/abort", headers=_auth(), timeout=TIMEOUT)
    _set_general(plugin_id, ids[V1])


def _set_general(plugin_id, version_id):
    r = httpx.post(f"{BASE}/admin/catalog/{plugin_id}/general-version", headers=_auth(),
                   data={"version_id": str(version_id)}, timeout=TIMEOUT, follow_redirects=False)
    assert r.status_code in (200, 303), r.text[:300]


# ── Canary : publier ne diffuse pas ──────────────────────────────────────

def test_publishing_without_general_keeps_every_native_channel_on_v1(published):
    assert _general() == V1
    assert _feed_version() == V1
    assert _download_version() == V1


def test_catalog_page_shows_the_general_version(published):
    page = _get(f"/catalog/{SLUG}").text
    assert f"v{V1}" in page and f"v{V2}" not in page


def test_feed_confirms_exactly_the_requested_version(published):
    assert _feed_version(f"?version={V2}") == V2
    assert _feed_version(f"?version={V1}") == V1
    assert _feed_version("?version=0.0.0.404") == 404
    assert _feed_version('?version=1"><x') == 400


def test_feed_links_are_absolute_https_and_pinned(published):
    r = _get(f"/catalog/{SLUG}/update.xml?version={V2}")
    href = ET.fromstring(r.content).find("u:update-download/u:src", LO_NS).get(XLINK)
    assert href.startswith("https://") and href.endswith(f"/{SLUG}-{V2}.oxt")


# ── Directive ciblée : lien épinglé et empreinte ─────────────────────────

def test_directive_link_is_pinned_and_matches_its_checksum(published):
    headers = {"X-Client-UUID": f"it-general-{uuid.uuid4()}", "X-Plugin-Version": V1}
    r = _get(f"/config/{SLUG}/config.json?profile={PROFILE}", headers=headers)
    assert r.status_code == 200, r.text[:300]
    update = r.json().get("update") or {}
    assert update.get("target_version") == V2, update
    assert update["artifact_url"].endswith(f"/{SLUG}-{V2}.oxt"), update["artifact_url"]
    binary = httpx.get(f"{BASE}{update['artifact_url']}", timeout=TIMEOUT, follow_redirects=True)
    assert binary.status_code == 200
    assert update["checksum"] == "sha256:" + hashlib.sha256(binary.content).hexdigest()


# ── Généralisation puis retour ───────────────────────────────────────────

def test_making_v2_general_moves_every_channel_then_back(published, plugin_id):
    _set_general(plugin_id, published[V2])
    assert (_general(), _feed_version(), _download_version()) == (V2, V2, V2)
    _set_general(plugin_id, published[V1])
    assert (_general(), _feed_version(), _download_version()) == (V1, V1, V1)


def test_general_version_api_refuses_without_token(published, plugin_id):
    r = httpx.post(f"{BASE}/admin/catalog/{plugin_id}/general-version",
                   data={"version_id": str(published[V2])}, timeout=TIMEOUT, follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 401, 403), r.status_code
    assert _general() == V1
