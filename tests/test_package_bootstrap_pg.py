"""Intégration (vrai Postgres) — un paquet servi par un DM ramène ses postes
vers ce DM (issue #133).

De bout en bout, par les routes HTTP : envoi d'un paquet dont la liste de DM
nomme une autre instance, puis ce que le DM stocke, sert, annonce (/config) et
montre dans la fiche de la version.

Marqué `integration`. Même montage que tests/test_deploy_guidance_pg.py, dont
ce fichier reprend le site de test (même parcours d'envoi que l'issue #132).

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_package_bootstrap_pg.py -m integration`
"""
# ruff: noqa: F811 — les fixtures importées sont redéclarées en paramètres de test
from __future__ import annotations

import hashlib
import io
import os
import zipfile

import pytest

from tests.test_deploy_guidance_pg import (  # noqa: F401 — fixtures pytest
    _DEVICE_TYPE,
    _SLUG,
    _TOKEN,
    _db_url,
    _page,
    mod,
    site,
)

pytestmark = pytest.mark.integration

# Le client de test joint l'application sous ce nom : un poste qui suit la liste
# du paquet servi arrive donc réellement sur ce DM.
HERE = "http://testserver"
ELSEWHERE = "https://dm-ailleurs.example"

_PREFS = (
    "pref('extensions.IAssistant.bootstrapUrls', '{urls}');\n"
    "pref('extensions.IAssistant.bootstrapUrl', '');\n"
)


def _xpi(urls: str, signed: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("install.rdf", (2026, 1, 1, 0, 0, 0)), "<RDF/>")
        zf.writestr(zipfile.ZipInfo("defaults/preferences/prefs.js", (2026, 1, 1, 0, 0, 0)),
                    _PREFS.format(urls=urls))
        if signed:
            zf.writestr(zipfile.ZipInfo("META-INF/mozilla.rsa", (2026, 1, 1, 0, 0, 0)), "signature")
    return buf.getvalue()


def _package_list(content: bytes) -> list[str]:
    """La liste de DM qu'un poste lirait dans le paquet."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        prefs = zf.read("defaults/preferences/prefs.js").decode("utf-8")
    line = next(ln for ln in prefs.splitlines() if ".bootstrapUrls'" in ln)
    return [u for u in line.split("'")[3].split(",") if u]


def _upload(site, version: str, package: bytes, **fields):
    data = {"version": version, "action": "deploy", "environment": "it",
            "deploy_strategy": "patch_all", "target_mode": "all"}
    data.update(fields)
    return site.http.post(
        f"/admin/catalog/{site.plugin_id}/versions/upload", data=data,
        files={"binary": (f"{_SLUG}-{version}.xpi", package, "application/octet-stream")},
        headers={"x-admin-token": _TOKEN}, follow_redirects=False)


def _served(site, version: str) -> bytes:
    res = site.http.get(f"/catalog/{_SLUG}/download/{_SLUG}-{version}.xpi")
    assert res.status_code == 200, res.text
    return res.content


def _relevé(site, version: str) -> dict | None:
    return site.sql("SELECT bootstrap_info FROM artifacts WHERE device_type = %s AND version = %s",
                    (_DEVICE_TYPE, version))[0][0]


@pytest.fixture
def instance(monkeypatch):
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public.example")


def test_e2e_package_naming_another_dm_is_rewritten_and_brings_devices_back(site, instance):
    res = _upload(site, "1.0.0", _xpi(f"{ELSEWHERE},https://autre.example"))
    assert res.status_code == 303
    assert "bootstrap_notice=" in res.headers["location"], "l'écart ne passe pas en silence"

    # Le paquet servi porte la liste de l'instance, et rien d'autre n'a bougé.
    served = _served(site, "1.0.0")
    assert _package_list(served) == [HERE]
    with zipfile.ZipFile(io.BytesIO(served)) as zf:
        assert zf.read("defaults/preferences/prefs.js").decode() == _PREFS.format(urls=HERE)

    # L'empreinte annoncée par la directive est celle du paquet réécrit.
    update = site.config_update("0.9.0")
    assert update["target_version"] == "1.0.0"
    assert update["checksum"] == "sha256:" + hashlib.sha256(served).hexdigest()
    assert _served(site, "1.0.0") == site.http.get(update["artifact_url"]).content

    # Un poste qui installe ce paquet interroge bien ce DM.
    [first] = _package_list(served)
    config = site.http.get(f"{first}/config/{_SLUG}/config.json",
                           headers={"X-Plugin-Version": "1.0.0", "X-Client-UUID": "00000000-0000-4000-8000-000000000133"})
    assert config.status_code == 200 and config.json()["config"]["bootstrapUrls"] == [HERE]

    info = _relevé(site, "1.0.0")
    assert info["status"] == "rewritten"
    assert info["package_urls"] == [ELSEWHERE, "https://autre.example"] and info["served_urls"] == [HERE]

    # La fiche le dit au retour de l'envoi, et le montre ensuite dans la version.
    page = _page(site, res.headers["location"])
    assert 'id="bootstrap-notice"' in page and "reecrite a l'envoi" in page
    assert ELSEWHERE in page and f"<code>{HERE}</code>" in page
    assert "n'envoie pas les postes vers ce DM" not in page


def test_e2e_package_already_right_is_served_without_warning(site, instance):
    res = _upload(site, "1.0.0", _xpi(f"{HERE}/"))
    assert res.status_code == 303 and "bootstrap_notice" not in res.headers["location"]
    assert _package_list(_served(site, "1.0.0")) == [f"{HERE}/"], "contenu inchangé"
    assert _relevé(site, "1.0.0")["status"] == "aligned"

    page = _page(site, f"/admin/catalog/{site.plugin_id}")
    assert 'id="bootstrap-notice"' not in page and "reecrite a l'envoi" not in page
    assert "n'envoie pas les postes vers ce DM" not in page
    assert f"DM du paquet : <code>{HERE}</code>" in page


def test_e2e_instance_list_comes_from_public_base_url_by_default(site, monkeypatch):
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public.example/dm/")
    assert _upload(site, "1.0.0", _xpi(ELSEWHERE)).status_code == 303
    assert _package_list(_served(site, "1.0.0")) == ["https://public.example/dm"]


def test_e2e_api_deploy_rewrites_and_says_so(site, instance):
    def deploy(version, package, **fields):
        return site.http.post(
            f"/api/plugins/{_SLUG}/deploy",
            data={"version": version, "strategy": "immediate", **fields},
            files={"binary": (f"{_SLUG}-{version}.xpi", package, "application/octet-stream")},
            headers={"X-Admin-Token": _TOKEN})

    res = deploy("2.0.0", _xpi(ELSEWHERE))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["bootstrap"]["status"] == "rewritten"
    assert body["bootstrap"]["package_urls"] == [ELSEWHERE] and body["bootstrap"]["served_urls"] == [HERE]
    assert len(body["warnings"]) == 1 and ELSEWHERE in body["warnings"][0]

    # Le binaire stocké (ici sans pod d'administration pour le resservir) est le
    # paquet réécrit, et c'est son empreinte que la directive annonce.
    with open(os.path.join(site.mod._binaries_dir_for_tests, _DEVICE_TYPE,
                           f"2.0.0_{_SLUG}-2.0.0.xpi"), "rb") as fh:
        stored = fh.read()
    assert _package_list(stored) == [HERE]
    update = site.config_update("1.9.0")
    assert update["checksum"] == body["checksum"] == "sha256:" + hashlib.sha256(stored).hexdigest()

    quiet = deploy("2.1.0", _xpi(HERE)).json()
    assert quiet["bootstrap"]["status"] == "aligned" and quiet["warnings"] == []

    # Paquet signé : refusé tant que sa conservation n'est pas demandée.
    refused = deploy("2.2.0", _xpi(ELSEWHERE, signed=True))
    assert refused.status_code == 409 and refused.json()["code"] == "bootstrap_signed"
    assert site.sql("SELECT 1 FROM artifacts WHERE device_type = %s AND version = '2.2.0'", (_DEVICE_TYPE,)) == []
    kept = deploy("2.2.0", _xpi(ELSEWHERE, signed=True), keep_bootstrap="true")
    assert kept.status_code == 201 and kept.json()["bootstrap"]["status"] == "kept"
    assert ELSEWHERE in kept.json()["warnings"][0]


def test_e2e_signed_package_is_refused_then_kept_on_confirmation_and_shown(site, instance):
    refused = _upload(site, "1.0.0", _xpi(ELSEWHERE, signed=True))
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "bootstrap_signed"
    assert "signé" in refused.json()["detail"]["message"]
    assert site.stored_files() == [] and site.versions() == {}, "refus : rien n'est stocké"

    kept = _upload(site, "1.0.0", _xpi(ELSEWHERE, signed=True), keep_bootstrap="1")
    assert kept.status_code == 303 and "bootstrap_notice=" in kept.headers["location"]
    assert _package_list(_served(site, "1.0.0")) == [ELSEWHERE], "jamais réécrit"

    page = _page(site, kept.headers["location"])
    assert "n'envoie pas les postes vers ce DM" in page and 'id="bootstrap-notice"' in page

    # Signé mais correct : rien à confirmer.
    assert _upload(site, "1.1.0", _xpi(HERE, signed=True)).status_code == 303


def test_e2e_version_sheet_shows_a_gap_that_appears_after_the_upload(site, instance, monkeypatch):
    assert _upload(site, "1.0.0", _xpi(ELSEWHERE)).status_code == 303
    assert "n'envoie pas les postes vers ce DM" not in _page(site, f"/admin/catalog/{site.plugin_id}")

    # La liste de l'instance change après l'envoi : le paquet stocké porte l'ancienne.
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "https://nouveau.example")
    page = _page(site, f"/admin/catalog/{site.plugin_id}")
    assert "n'envoie pas les postes vers ce DM" in page and f"<code>{HERE}</code>" in page


def test_e2e_version_sheet_reads_a_package_sent_before_the_check(site, monkeypatch):
    # Instance sans adresse connue : pas de réécriture, mais l'envoi dit où iront les postes.
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    res = _upload(site, "1.0.0", _xpi(ELSEWHERE))
    assert res.status_code == 303 and "bootstrap_notice=" in res.headers["location"]
    assert _relevé(site, "1.0.0")["status"] == "instance_unknown"
    assert _package_list(_served(site, "1.0.0")) == [ELSEWHERE]

    # Paquet d'avant le contrôle (aucun relevé en base) : la fiche lit le fichier.
    site.sql("UPDATE artifacts SET bootstrap_info = NULL WHERE device_type = %s", (_DEVICE_TYPE,))
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)
    page = _page(site, f"/admin/catalog/{site.plugin_id}")
    assert f"<code>{ELSEWHERE}</code>" in page and "n'envoie pas les postes vers ce DM" in page

    # Le renvoyer suffit : sa liste est réécrite (binaire différent → remplacement confirmé).
    assert _upload(site, "1.0.0", _xpi(ELSEWHERE)).status_code == 409
    assert _upload(site, "1.0.0", _xpi(ELSEWHERE), confirm_overwrite="1").status_code == 303
    assert _package_list(_served(site, "1.0.0")) == [HERE]


def test_e2e_raw_artifact_upload_is_aligned_too(site, instance):
    res = site.http.post(
        "/api/artifacts", data={"device_type": _DEVICE_TYPE, "version": "4.0.0"},
        files={"binary": (f"{_SLUG}-4.0.0.xpi", _xpi(ELSEWHERE), "application/octet-stream")},
        headers={"X-Admin-Token": _TOKEN})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["bootstrap"]["status"] == "rewritten" and body["warnings"]
    [(path, checksum)] = site.sql(
        "SELECT s3_path, checksum FROM artifacts WHERE device_type = %s AND version = '4.0.0'", (_DEVICE_TYPE,))
    with open(os.path.join(site.mod._binaries_dir_for_tests, path), "rb") as fh:
        stored = fh.read()
    assert _package_list(stored) == [HERE] and checksum == "sha256:" + hashlib.sha256(stored).hexdigest()


def test_e2e_legacy_deploy_form_rewrites_too(site, instance):
    _page(site, "/admin/campaigns")     # pose la session d'administration
    res = site.http.post(
        "/admin/deploy/create", follow_redirects=False,
        data={"device_type": _DEVICE_TYPE, "version": "3.0.0", "deploy_type": "immediate",
              "target_mode": "all", "name": _SLUG},
        files={"binary": (f"{_SLUG}-3.0.0.xpi", _xpi(ELSEWHERE), "application/octet-stream")})
    assert res.status_code == 303, res.text
    assert _relevé(site, "3.0.0")["status"] == "rewritten"
    [(path, checksum)] = site.sql(
        "SELECT s3_path, checksum FROM artifacts WHERE device_type = %s AND version = '3.0.0'", (_DEVICE_TYPE,))
    with open(path, "rb") as fh:
        stored = fh.read()
    assert _package_list(stored) == [HERE]
    assert checksum == "sha256:" + hashlib.sha256(stored).hexdigest()
    assert site.config_update("2.9.0")["checksum"] == checksum
