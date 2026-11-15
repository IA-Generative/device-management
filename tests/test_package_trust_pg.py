"""Intégration (vrai Postgres) — chaîne de confiance des paquets (ADR-0006).

De bout en bout, par les routes HTTP : un paquet neutre et sa signature
d'origine sont envoyés, le DM les vérifie, adapte le paquet, le signe, et ce
qu'il annonce à un poste (/config), sert au catalogue et montre dans la fiche
de la version se vérifie avec la seule ancre déposée dans le paquet.

Marqué `integration`. Même montage que tests/test_deploy_guidance_pg.py.

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_package_trust_pg.py -m integration`
"""
# ruff: noqa: F811 — les fixtures importées sont redéclarées en paramètres de test
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from app.services import package_signing as ps
from tests.package_trust_helpers import (
    build_envelope,
    delegation,
    new_key,
    private_pem,
    public_pem,
    statement_of,
    xpi,
)
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

HERE = "http://testserver"


@pytest.fixture
def chain(monkeypatch):
    """Instance exigeante : origine signée obligatoire, DM signataire par délégation."""
    publisher, root, dm = new_key(), new_key(), new_key()
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    monkeypatch.setenv("DM_PACKAGE_PUBLISHER_KEYS", public_pem(publisher))
    monkeypatch.setenv("DM_PACKAGE_ALLOWED_REFS", "refs/heads/main,refs/heads/develop")
    monkeypatch.setenv("DM_PACKAGE_SIGNING_REQUIRED", "true")
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", private_pem(dm))
    monkeypatch.setenv("DM_PACKAGE_TRUST_ROOTS", public_pem(root))
    monkeypatch.setenv("DM_PACKAGE_SIGNING_DELEGATION", json.dumps(delegation(root, dm, dm=HERE)))
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "client-de-l-instance")
    return {"publisher": publisher, "root": root, "dm": dm}


def _send(site, chain, version, *, package=None, ref="refs/heads/main", signer=None, signed=True, **fields):
    package = package if package is not None else xpi(version)
    data = {"version": version, "action": "deploy", "environment": "it",
            "deploy_strategy": "patch_all", "target_mode": "all"}
    data.update(fields)
    files = {"binary": (f"{_SLUG}-{version}.xpi", package, "application/octet-stream")}
    if signed:
        envelope = build_envelope(signer or chain["publisher"], package, slug=_SLUG, version=version, ref=ref)
        files["signature"] = ("paquet.sig.json", json.dumps(envelope), "application/json")
    return site.http.post(f"/admin/catalog/{site.plugin_id}/versions/upload", data=data, files=files,
                          headers={"x-admin-token": _TOKEN}, follow_redirects=False)


def _artifact(site, version):
    return site.sql("SELECT signature, provenance, bootstrap_info FROM artifacts WHERE device_type = %s AND version = %s",
                    (_DEVICE_TYPE, version))[0]


def _verify_like_a_device(package: bytes, envelope: dict, version: str) -> dict:
    """Ce que fait le plugin : l'ancre vient du paquet, rien d'autre n'est cru sur parole."""
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        trust = json.loads(zf.read(ps.TRUST_FILE))
    roots = {r["keyid"]: ps.public_key_from_b64(r["public_key"]) for r in trust["roots"]}
    statement, _signer = ps.verify_envelope(envelope, roots)
    assert statement["kind"] == "served"
    assert statement["sha256"] == hashlib.sha256(package).hexdigest()
    assert statement["version"] == version
    return statement


def test_e2e_neutral_signed_package_is_verified_adapted_signed_and_served(site, chain):
    config = {"prod": {"keycloakClientId": "client-du-build"}, "local": {"x": 1}}
    res = _send(site, chain, "1.0.0", package=xpi("1.0.0", config=config))
    assert res.status_code == 303, res.text

    # Ce que le DM a retenu.
    signature, provenance, bootstrap = _artifact(site, "1.0.0")
    assert provenance["build"]["status"] == "verified" and provenance["build"]["ref"] == "refs/heads/main"
    assert provenance["nature"] == "general" and provenance["trust_anchor"] is True
    assert provenance["config"]["replaced"] == ["prod.keycloakClientId"]
    assert bootstrap["status"] == "rewritten" and "signature" not in bootstrap

    # Ce qu'un poste reçoit et vérifie.
    update = site.config_update("0.9.0")
    assert update["target_version"] == "1.0.0" and update["signature"] == signature
    package = site.http.get(update["artifact_url"]).content
    assert update["checksum"] == "sha256:" + hashlib.sha256(package).hexdigest()
    statement = _verify_like_a_device(package, update["signature"], "1.0.0")
    assert statement["upstream"]["verified"] is True and statement["dm"] == HERE
    assert statement["extension_id"] == "plugin@example.invalid"
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        assert HERE in zf.read("defaults/preferences/prefs.js").decode()
        assert "dm-config.json" not in zf.namelist()
        assert [r["keyid"] for r in json.loads(zf.read(ps.TRUST_FILE))["roots"]] == [ps.key_id(chain["root"].public_key())]

    # La configuration du plugin ne porte plus la valeur du build.
    template = site.sql("SELECT config_template FROM plugins WHERE id = %s", (site.plugin_id,))[0][0]
    template = json.loads(template) if isinstance(template, str) else template
    assert template["prod"]["keycloakClientId"] == "${{KEYCLOAK_CLIENT_ID}}" and template["local"] == {"x": 1}

    # L'enveloppe est servie à côté du paquet, pour qui installe à la main.
    res = site.http.get(f"/catalog/{_SLUG}/download/{_SLUG}-1.0.0.xpi.sig.json")
    assert res.status_code == 200 and res.json() == signature

    # La fiche de la version dit la nature, l'origine et la signature.
    page = _page(site, f"/admin/catalog/{site.plugin_id}?tab=versions")
    assert "Origine vérifiée" in page and "Signee par ce DM" in page and "Générale" in page


@pytest.mark.parametrize("case,status,code", [
    ("unsigned", 422, "signature_required"),
    ("unknown_signer", 422, "signature_required"),
    ("tampered", 422, "signature_invalid"),
    ("wrong_version", 422, "version_mismatch"),
])
def test_e2e_refusals_store_nothing(site, chain, case, status, code):
    version, package = "1.0.0", xpi("1.0.0")
    if case == "unsigned":
        res = _send(site, chain, version, package=package, signed=False)
    elif case == "unknown_signer":
        res = _send(site, chain, version, package=package, signer=new_key())
    elif case == "tampered":
        envelope = build_envelope(chain["publisher"], package, slug=_SLUG, version=version)
        res = site.http.post(
            f"/admin/catalog/{site.plugin_id}/versions/upload",
            data={"version": version, "action": "deploy", "environment": "it", "target_mode": "all"},
            files={"binary": (f"{_SLUG}-{version}.xpi", xpi(version, extra={"porte.js": "derobee"}), "application/octet-stream"),
                   "signature": ("s.json", json.dumps(envelope), "application/json")},
            headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    else:
        res = _send(site, chain, "1.0.1", package=package)
    assert res.status_code == status, res.text
    assert res.json()["detail"]["code"] == code
    assert site.versions() == {} and site.stored_files() == []


def test_e2e_trial_from_an_unlisted_branch_needs_a_recorded_confirmation(site, chain):
    fields = dict(ref="refs/heads/exp/essai", campaign_kind="experiment", target_mode="emails",
                  emails="testeur@example.test", tag="exp-essai", hypotheses="Est-ce plus clair ?")
    res = _send(site, chain, "1.1.0-dev.12", **fields)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "ref_not_allowed"
    assert site.versions() == {}

    res = _send(site, chain, "1.1.0-dev.12", confirm_ref="1", **fields)
    assert res.status_code == 303, res.text
    assert site.versions() == {"1.1.0-dev.12": "experimental"}
    _signature, provenance, _bootstrap = _artifact(site, "1.1.0-dev.12")
    assert provenance["nature"] == "dev" and provenance["build"]["ref_confirmed"] is True
    audit = site.sql("SELECT payload FROM admin_audit_log WHERE action = 'version.upload' ORDER BY id DESC LIMIT 1")[0][0]
    assert audit["bootstrap"]["build"]["ref_confirmed"] is True and audit["bootstrap"]["signed"] is True
    assert "payload" not in json.dumps(audit["bootstrap"]), "le journal ne garde pas l'enveloppe"

    # Le testeur reçoit l'essai signé ; le reste du parc ne reçoit rien.
    update = site.config_update("1.0.0", email="testeur@example.test")
    assert update["target_version"] == "1.1.0-dev.12"
    _verify_like_a_device(site.http.get(update["artifact_url"]).content, update["signature"], "1.1.0-dev.12")
    assert site.config_update("1.0.0", email="autre@example.test") is None

    # Inviter : la personne ajoutée à la cohorte de l'essai le reçoit à son tour.
    version_id = site.sql("SELECT id FROM plugin_versions WHERE plugin_id = %s AND version = '1.1.0-dev.12'",
                          (site.plugin_id,))[0][0]
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/versions/{version_id}/invite",
                         data={"emails": "autre@example.test, pas-une-adresse"},
                         headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 400
    # Une cohorte qui n'est pas une liste de personnes : l'ajout n'aurait aucun effet, il est refusé.
    cohort_id = site.sql("SELECT target_cohort_id FROM campaigns WHERE plugin_id = %s AND is_experiment",
                         (site.plugin_id,))[0][0]
    site.sql("UPDATE cohorts SET type = 'email_pattern' WHERE id = %s", (cohort_id,))
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/versions/{version_id}/invite",
                         data={"emails": "autre@example.test"},
                         headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 409
    site.sql("UPDATE cohorts SET type = 'manual' WHERE id = %s", (cohort_id,))
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/versions/{version_id}/invite",
                         data={"emails": "Autre@example.test"},
                         headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 303 and f"invited={version_id}" in res.headers["location"]
    assert site.config_update("1.0.0", email="autre@example.test")["target_version"] == "1.1.0-dev.12"

    page = _page(site, f"/admin/catalog/{site.plugin_id}?tab=versions&invited={version_id}")
    assert "?exp=exp-essai" in page and "Est-ce plus clair ?" in page and "(sur confirmation)" in page


def test_e2e_candidate_is_public_only_when_the_admin_says_so(site, chain):
    assert _send(site, chain, "1.0.0").status_code == 303
    res = _send(site, chain, "1.1.0-rc.3", ref="refs/heads/develop", action="experimental", tag="rc")
    assert res.status_code == 303, res.text
    res = _send(site, chain, "1.1.0-dev.9", ref="refs/heads/develop", action="experimental", tag="exp-secret")
    assert res.status_code == 303, res.text

    page = site.http.get(f"/catalog/{_SLUG}").text
    assert "1.1.0-rc.3" not in page and "1.1.0-dev.9" not in page and "sur invitation" in page

    res = site.http.post(f"/admin/catalog/{site.plugin_id}/candidate-visibility", data={"show": "1"},
                         headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 303
    page = site.http.get(f"/catalog/{_SLUG}").text
    assert "Preversion 1.1.0-rc.3" in page and "?exp=rc&amp;v=1.1.0-rc.3" in page
    assert "1.1.0-dev.9" not in page and "exp-secret" not in page, "un essai n'est jamais listé"

    # Un essai rangé par erreur sous l'étiquette de la candidate ne sort pas par le lien public.
    res = _send(site, chain, "1.1.0-dev.4", ref="refs/heads/develop", action="experimental", tag="rc")
    assert res.status_code == 303, res.text
    page = site.http.get(f"/catalog/{_SLUG}?exp=rc&v=1.1.0-rc.3").text
    assert "1.1.0-dev.4" not in page
    assert "Version d'essai" in page and "Revenir a la version generale" in page
    # Le bouton principal sert l'essai, pas la version générale.
    assert "Telecharger la version d'essai 1.1.0-rc.3" in page and "Version generale 1.0.0" in page


def test_e2e_prerelease_in_an_ordinary_campaign_is_confirmed_on_production(site, chain, monkeypatch):
    monkeypatch.setenv("DM_APP_ENV", "prod")
    res = _send(site, chain, "1.1.0-rc.4", ref="refs/heads/develop")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "prerelease_ordinary"
    assert site.versions() == {} and site.stored_files() == []
    res = _send(site, chain, "1.1.0-rc.4", ref="refs/heads/develop", confirm_prerelease="1")
    assert res.status_code == 303, res.text


def test_e2e_api_deploy_follows_the_same_rules(site, chain):
    package = xpi("2.0.0")
    res = site.http.post(f"/api/plugins/{_SLUG}/deploy", headers={"X-Admin-Token": _TOKEN},
                         data={"strategy": "immediate", "version": "2.0.0"},
                         files={"binary": (f"{_SLUG}-2.0.0.xpi", package, "application/octet-stream")})
    assert res.status_code == 422 and res.json()["code"] == "signature_required"
    envelope = build_envelope(chain["publisher"], package, slug=_SLUG, version="2.0.0")
    res = site.http.post(f"/api/plugins/{_SLUG}/deploy", headers={"X-Admin-Token": _TOKEN},
                         data={"strategy": "immediate", "version": "2.0.0"},
                         files={"binary": (f"{_SLUG}-2.0.0.xpi", package, "application/octet-stream"),
                                "signature": ("s.json", json.dumps(envelope), "application/json")})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["signed"] is True and body["provenance"]["build"]["status"] == "verified"
    assert statement_of(_artifact(site, "2.0.0")[0])["version"] == "2.0.0"


def test_e2e_a_component_without_the_key_refuses_the_import(site, chain, monkeypatch):
    monkeypatch.delenv("DM_PACKAGE_SIGNING_KEY")
    res = _send(site, chain, "1.0.0")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "signing_config"
    assert site.versions() == {}


def test_e2e_without_any_setting_nothing_changes(site, monkeypatch):
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)
    res = site.http.post(
        f"/admin/catalog/{site.plugin_id}/versions/upload",
        data={"version": "1.0.0", "action": "deploy", "environment": "it", "target_mode": "all"},
        files={"binary": (f"{_SLUG}-1.0.0.xpi", xpi("1.0.0"), "application/octet-stream")},
        headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 303, res.text
    signature, provenance, _bootstrap = _artifact(site, "1.0.0")
    assert signature is None and provenance["build"]["status"] == "unchecked"
    update = site.config_update("0.9.0")
    assert "signature" not in update, "sans clé, la directive est celle d'avant"
    assert site.http.get(f"/catalog/{_SLUG}/download/{_SLUG}-1.0.0.xpi.sig.json").status_code == 404


def test_e2e_a_campaign_can_ask_for_a_restart_after_install(site, monkeypatch):
    """La directive ne porte `restart_required` que si le déploiement l'a demandé."""
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)

    def send(version, **fields):
        return site.http.post(
            f"/admin/catalog/{site.plugin_id}/versions/upload",
            data={"version": version, "action": "deploy", "environment": "it", "target_mode": "all",
                  "deploy_strategy": "patch_all", **fields},
            files={"binary": (f"{_SLUG}-{version}.xpi", xpi(version), "application/octet-stream")},
            headers={"x-admin-token": _TOKEN}, follow_redirects=False)

    assert send("1.0.0").status_code == 303
    assert "restart_required" not in site.config_update("0.9.0"), "sans demande, la directive est inchangée"

    assert send("1.1.0", restart_required="1").status_code == 303
    update = site.config_update("1.0.0")
    assert update["target_version"] == "1.1.0" and update["restart_required"] is True
    audit = site.sql("SELECT payload FROM admin_audit_log WHERE action = 'deploy.create' ORDER BY id DESC LIMIT 1")
    assert audit and audit[0][0]["restart_required"] is True
    page = _page(site, f"/admin/catalog/{site.plugin_id}?tab=versions")
    assert "redemarrage demande" in page

    # Par l'API aussi.
    res = site.http.post(f"/api/plugins/{_SLUG}/deploy", headers={"X-Admin-Token": _TOKEN},
                         data={"strategy": "immediate", "version": "1.2.0", "restart_required": "true"},
                         files={"binary": (f"{_SLUG}-1.2.0.xpi", xpi("1.2.0"), "application/octet-stream")})
    assert res.status_code == 201, res.text
    assert site.config_update("1.1.0")["restart_required"] is True


# ─── Constats de relecture ───────────────────────────────────────────────────

def _stored_zip_with_bad_crc(version: str) -> bytes:
    """Archive ouvrable dont install.rdf ne se lit pas (contenu changé, CRC inchangé)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("install.rdf", (2026, 1, 1, 0, 0, 0)),
                    f"<RDF><em:id>p@x</em:id><em:version>{version}</em:version></RDF>")
    return buf.getvalue().replace(f"<em:version>{version}".encode(), b"<em:version>" + b"9" * len(version))


def test_e2e_require_is_not_bypassed_by_an_archive_that_reads_badly(site, chain):
    """Une archive qui se lit mal était « stockée telle quelle » avant tout contrôle."""
    package = _stored_zip_with_bad_crc("3.0.0")
    for url, data in ((f"/api/plugins/{_SLUG}/deploy", {"strategy": "immediate", "version": "3.0.0"}),
                      ("/api/artifacts", {"device_type": _DEVICE_TYPE, "version": "3.0.0"})):
        res = site.http.post(url, headers={"X-Admin-Token": _TOKEN}, data=data,
                             files={"binary": (f"{_SLUG}-3.0.0.xpi", package, "application/octet-stream")})
        assert res.status_code == 422, (url, res.text)
    # Même signée par la chaîne de build, elle n'est pas servie : elle ne peut pas être signée par le DM.
    envelope = build_envelope(chain["publisher"], package, slug=_SLUG, version="3.0.0")
    res = site.http.post(f"/api/plugins/{_SLUG}/deploy", headers={"X-Admin-Token": _TOKEN},
                         data={"strategy": "immediate", "version": "3.0.0"},
                         files={"binary": (f"{_SLUG}-3.0.0.xpi", package, "application/octet-stream"),
                                "signature": ("s.json", json.dumps(envelope), "application/json")})
    assert res.status_code == 422 and res.json()["code"] == "unreadable_archive", res.text
    assert site.versions() == {} and site.stored_files() == []
    assert site.sql("SELECT count(*) FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,))[0][0] == 0


def test_e2e_paths_that_do_not_name_the_plugin_are_refused_under_require(site, chain):
    package = xpi("1.0.0")
    envelope = json.dumps(build_envelope(chain["publisher"], package, slug="un-autre-plugin", version="1.0.0"))
    res = site.http.post("/api/artifacts", headers={"X-Admin-Token": _TOKEN},
                         data={"device_type": _DEVICE_TYPE, "version": "1.0.0"},
                         files={"binary": (f"{_SLUG}-1.0.0.xpi", package, "application/octet-stream"),
                                "signature": ("s.json", envelope, "application/json")})
    assert res.status_code == 422 and res.json()["code"] == "plugin_required", res.text
    _page(site, "/admin/campaigns")     # pose la session d'administration
    res = site.http.post("/admin/deploy/create", follow_redirects=False,
                         data={"device_type": _DEVICE_TYPE, "version": "1.0.0", "target_mode": "all"},
                         files={"binary": (f"{_SLUG}-1.0.0.xpi", package, "application/octet-stream"),
                                "signature": ("s.json", envelope, "application/json")})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "plugin_required", res.text
    assert site.sql("SELECT count(*) FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,))[0][0] == 0


def test_e2e_api_import_leaves_an_audit_line(site, chain, monkeypatch):
    monkeypatch.setenv("DM_PACKAGE_ALLOWED_REFS", "refs/heads/main")
    package = xpi("2.1.0")
    envelope = build_envelope(chain["publisher"], package, slug=_SLUG, version="2.1.0", ref="refs/heads/exp/x")
    res = site.http.post(f"/api/plugins/{_SLUG}/deploy", headers={"X-Admin-Token": _TOKEN},
                         data={"strategy": "immediate", "version": "2.1.0", "confirm_ref": "true"},
                         files={"binary": (f"{_SLUG}-2.1.0.xpi", package, "application/octet-stream"),
                                "signature": ("s.json", json.dumps(envelope), "application/json")})
    assert res.status_code == 201, res.text
    audit = site.sql("SELECT payload FROM admin_audit_log WHERE action = 'package.import' ORDER BY id DESC LIMIT 1")
    assert audit, "un import par l'API doit laisser une ligne d'audit"
    payload = audit[0][0]
    assert payload["build"]["ref_confirmed"] is True and payload["build"]["ref"] == "refs/heads/exp/x"
    assert payload["signed"] is True and "payload" not in json.dumps(payload["build"])


def test_e2e_the_served_envelope_carries_the_delegation_in_force(site, chain, monkeypatch):
    assert _send(site, chain, "1.0.0").status_code == 303
    stored = _artifact(site, "1.0.0")[0]
    renewed = delegation(chain["root"], chain["dm"], days_after=700, dm=HERE)
    monkeypatch.setenv("DM_PACKAGE_SIGNING_DELEGATION", json.dumps(renewed))
    update = site.config_update("0.9.0")
    assert update["signature"]["delegation"] == renewed != stored["delegation"]
    assert update["signature"]["signatures"] == stored["signatures"], "seule la délégation change"
    served = site.http.get(f"/catalog/{_SLUG}/download/{_SLUG}-1.0.0.xpi.sig.json").json()
    assert served["delegation"] == renewed
    _verify_like_a_device(site.http.get(update["artifact_url"]).content, update["signature"], "1.0.0")


def test_e2e_legacy_deploy_form_also_confirms_a_prerelease_on_production(site, monkeypatch):
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)
    monkeypatch.setenv("DM_APP_ENV", "prod")
    fields = {"device_type": _DEVICE_TYPE, "version": "1.2.0-rc.1", "target_mode": "all", "deploy_type": "immediate"}
    files = {"binary": (f"{_SLUG}-1.2.0-rc.1.xpi", xpi("1.2.0-rc.1"), "application/octet-stream")}
    _page(site, "/admin/campaigns")     # pose la session d'administration
    res = site.http.post("/admin/deploy/create", data=fields, files=files, follow_redirects=False)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "prerelease_ordinary", res.text
    assert site.sql("SELECT count(*) FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,))[0][0] == 0
