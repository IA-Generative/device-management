"""Import d'un paquet (ADR-0006, issues #106 et #107) — point de passage unique des envois."""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.services import package_bootstrap, package_config, package_import
from app.services import package_signing as ps
from app.services.versions import is_prerelease, prerelease_campaign_message, version_nature
from tests.package_trust_helpers import (
    build_envelope,
    new_key,
    private_pem,
    public_pem,
    statement_of,
    xpi,
)

HERE = "https://dm.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("DM_PACKAGE_SIGNATURE_POLICY", "DM_PACKAGE_PUBLISHER_KEYS", "DM_PACKAGE_ALLOWED_REFS",
                 "DM_PACKAGE_ALLOWED_REPOSITORIES", "DM_PACKAGE_SIGNING_KEY", "DM_PACKAGE_SIGNING_DELEGATION",
                 "DM_PACKAGE_TRUST_ROOTS", "DM_PACKAGE_SIGNING_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", HERE)


@pytest.fixture
def publisher(monkeypatch):
    key = new_key()
    monkeypatch.setenv("DM_PACKAGE_PUBLISHER_KEYS", public_pem(key))
    return key


@pytest.fixture
def signing(monkeypatch):
    key = new_key()
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", private_pem(key))
    return key


def _names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


# ─── Nature des versions (issue #106) ───────────────────────────────────────

@pytest.mark.parametrize("version,nature", [
    ("0.24.0", "general"), ("0.23.0.154", "general"), ("0.25.0-rc.241", "rc"), ("1.6.0-rc1", "rc"),
    ("0.25.0-dev.12", "dev"), ("0.25.0-dev.3+abc", "dev"), ("1.6.0-test3", "other"), ("v1.2", "other"),
    ("", "other"),
])
def test_version_nature(version, nature):
    assert version_nature(version) == nature
    assert is_prerelease(version) == (nature != "general")


def test_prerelease_in_an_ordinary_campaign_only_matters_in_production():
    assert prerelease_campaign_message("0.25.0-rc.1", "dev") is None
    assert prerelease_campaign_message("0.25.0", "prod") is None
    message = prerelease_campaign_message("0.25.0-rc.1", "Production")
    assert message and "candidate" in message


# ─── Sans politique ni clé : rien ne change ─────────────────────────────────

def test_default_behaviour_is_the_bootstrap_rewrite_only():
    data = xpi("1.0.0")
    served, info = package_import.import_package(data, slug="plugin", version="1.0.0", filename="plugin-1.0.0.xpi")
    expected, _ = package_bootstrap.prepare(data)
    assert served == expected, "sans clé de signature, le paquet servi est celui de l'issue #133"
    assert info["build"] == {"status": package_import.BUILD_UNCHECKED}
    assert info["signature"] is None and info["trust_anchor"] is False
    assert info["status"] == package_bootstrap.REWRITTEN and info["nature"] == "general"
    assert info["versions"] == {"install.rdf": "1.0.0"}


def test_split_info_separates_what_goes_in_each_column(signing):
    _served, info = package_import.import_package(xpi("1.0.0"), slug="p", version="1.0.0", filename="p-1.0.0.xpi")
    bootstrap, signature, provenance = package_import.split_info(info)
    assert signature["schema"] == ps.ENVELOPE_SCHEMA
    assert {"nature", "upstream", "build", "versions", "changed_files", "trust_anchor"} <= set(provenance)
    assert "signature" not in bootstrap and "build" not in bootstrap and bootstrap["status"] == "rewritten"
    assert package_import.split_info(None) == (None, None, None)


# ─── Concordance des numéros (issue #106) ───────────────────────────────────

def test_a_native_version_that_differs_is_refused_and_names_both():
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(xpi("1.0.0"), version="1.0.1", filename="plugin-1.0.1.xpi")
    assert e.value.code == package_import.VERSION_MISMATCH
    assert "1.0.0" in str(e.value) and "1.0.1" in str(e.value) and "install.rdf" in str(e.value)


def test_manifest_and_filename_gaps_are_only_noted():
    data = xpi("1.0.0", extra={"dm-manifest.json": json.dumps({"changelog": [{"version": "0.9.0"}]})})
    _served, info = package_import.import_package(data, version="1.0.0", filename="plugin-1.0.2.xpi")
    assert any("0.9.0" in n for n in info["notes"]) and any("1.0.2" in n for n in info["notes"])


def test_embedded_identity_is_the_extension_not_its_target_application():
    assert package_import.embedded_identity(xpi("1.0.0", extension_id="moi@exemple")) == "moi@exemple"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"version": "2.0", "browser_specific_settings": {"gecko": {"id": "g@x"}}}))
    assert package_import.embedded_identity(buf.getvalue()) == "g@x"
    assert package_import.embedded_versions(buf.getvalue()) == {"manifest.json": "2.0"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("description.xml", '<description><identifier value="lo.id"/><version value="0.3.1-rc.2"/></description>')
    assert package_import.embedded_identity(buf.getvalue()) == "lo.id"
    assert package_import.embedded_versions(buf.getvalue()) == {"description.xml": "0.3.1-rc.2"}
    assert package_import.embedded_identity(b"pas une archive") == ""


# ─── Signature d'origine selon la politique ─────────────────────────────────

def test_warn_policy_reports_but_lets_an_unsigned_package_in(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "warn")
    _served, info = package_import.import_package(xpi("1.0.0"), slug="plugin", version="1.0.0")
    assert info["build"]["status"] == package_import.BUILD_UNSIGNED


def test_require_policy_refuses_unsigned_and_unknown_signers(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    data = xpi("1.0.0")
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="plugin", version="1.0.0")
    assert e.value.code == package_import.SIGNATURE_REQUIRED
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="plugin", version="1.0.0",
                                      build_signature=build_envelope(new_key(), data))
    assert e.value.code == package_import.SIGNATURE_REQUIRED


@pytest.mark.parametrize("policy", ["warn", "require"])
def test_a_provided_signature_that_does_not_match_is_refused_whatever_the_policy(monkeypatch, publisher, policy):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", policy)
    data = xpi("1.0.0")
    envelope = build_envelope(publisher, data)
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(xpi("1.0.0", extra={"porte.js": "derobee"}), slug="plugin",
                                      version="1.0.0", build_signature=envelope)
    assert e.value.code == package_import.SIGNATURE_INVALID


def test_a_verified_package_records_where_it_comes_from(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    data = xpi("1.0.0")
    _served, info = package_import.import_package(data, slug="plugin", version="1.0.0",
                                                  build_signature=json.dumps(build_envelope(publisher, data)))
    build = info["build"]
    assert build["status"] == package_import.BUILD_VERIFIED and build["ref"] == "refs/heads/main"
    assert build["repository"] == "org/depot" and build["ref_allowed"] is True


def test_a_ref_outside_the_list_needs_a_confirmation_that_is_recorded(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    monkeypatch.setenv("DM_PACKAGE_ALLOWED_REFS", "refs/heads/main")
    data = xpi("1.0.0-dev.7")
    envelope = build_envelope(publisher, data, version="1.0.0-dev.7", ref="refs/heads/exp/essai")
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="plugin", version="1.0.0-dev.7", build_signature=envelope)
    assert e.value.code == package_import.REF_NOT_ALLOWED and e.value.info["build"]["ref_allowed"] is False
    _served, info = package_import.import_package(data, slug="plugin", version="1.0.0-dev.7",
                                                  build_signature=envelope, confirm_ref=True)
    assert info["build"]["ref_confirmed"] is True and info["nature"] == "dev"


# ─── Signature du DM ────────────────────────────────────────────────────────

def test_the_served_package_gets_the_anchor_and_a_signature_over_its_final_bytes(signing):
    data = xpi("1.0.0", config={"prod": {}})
    served, info = package_import.import_package(data, slug="plugin", version="1.0.0", filename="plugin-1.0.0.xpi")
    assert ps.TRUST_FILE in _names(served) and "dm-config.json" not in _names(served)
    statement = statement_of(info["signature"])
    import hashlib
    assert statement["sha256"] == hashlib.sha256(served).hexdigest()
    assert statement["upstream"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert statement["upstream"]["verified"] is False, "politique « off » : l'origine n'a pas été contrôlée"
    assert set(statement["changed_files"]) == {"dm-config.json", "dm-trust.json", "defaults/preferences/prefs.js"}
    assert statement["dm"] == HERE and statement["extension_id"] == "plugin@example.invalid"
    with zipfile.ZipFile(io.BytesIO(served)) as zf:
        trust = json.loads(zf.read(ps.TRUST_FILE))
    assert trust["roots"][0]["keyid"] == ps.key_id(signing.public_key())


def test_importing_the_same_package_twice_gives_the_same_bytes(signing):
    data = xpi("1.0.0")
    first, _ = package_import.import_package(data, slug="p", version="1.0.0")
    second, _ = package_import.import_package(data, slug="p", version="1.0.0")
    assert first == second, "sinon le second envoi est refusé : même numéro, autre binaire"


def test_an_anchor_shipped_inside_the_package_never_wins(signing):
    data = xpi("1.0.0", extra={"dm-trust.json": json.dumps({"schema": "dm-trust/1", "roots": [{"keyid": "intrus"}]})})
    served, _info = package_import.import_package(data, slug="p", version="1.0.0")
    with zipfile.ZipFile(io.BytesIO(served)) as zf:
        assert zf.namelist().count(ps.TRUST_FILE) == 1
        assert json.loads(zf.read(ps.TRUST_FILE))["roots"][0]["keyid"] == ps.key_id(signing.public_key())


def test_an_archive_with_an_embedded_signature_is_left_untouched_but_still_signed(signing):
    data = xpi("1.0.0", urls=HERE, extra={"META-INF/mozilla.rsa": "signature"})
    served, info = package_import.import_package(data, slug="p", version="1.0.0")
    assert ps.TRUST_FILE not in _names(served) and info["trust_anchor"] is False
    assert info["signature"] is not None


def test_a_component_without_the_key_refuses_when_signing_is_required(monkeypatch):
    monkeypatch.setenv("DM_PACKAGE_SIGNING_REQUIRED", "true")
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(xpi("1.0.0"), slug="p", version="1.0.0")
    assert e.value.code == package_import.SIGNING_CONFIG and "administration" in str(e.value)


def test_a_broken_signing_configuration_refuses_the_import(monkeypatch):
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", "pas une clé")
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(xpi("1.0.0"), slug="p", version="1.0.0")
    assert e.value.code == package_import.SIGNING_CONFIG


# ─── Aperçu ─────────────────────────────────────────────────────────────────

def test_preview_never_raises_and_says_whether_a_confirmation_would_do(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    monkeypatch.setenv("DM_PACKAGE_ALLOWED_REFS", "refs/heads/main")
    data = xpi("1.0.0-rc.3")
    out = package_import.preview(data, slug="plugin", version="1.0.0-rc.3")
    assert out["refusal"]["code"] == package_import.SIGNATURE_REQUIRED and out["refusal"]["confirmable"] is False
    out = package_import.preview(data, slug="plugin", version="1.0.0-rc.3",
                                 build_signature=build_envelope(publisher, data, version="1.0.0-rc.3",
                                                                ref="refs/heads/develop"))
    assert out["refusal"]["confirmable"] is True and out["build"]["ref"] == "refs/heads/develop"
    assert out["nature"] == "rc"
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "off")
    assert package_import.preview(data, version="9.9.9")["refusal"]["code"] == package_import.VERSION_MISMATCH
    assert package_import.preview(b"pas une archive", version="1.0.0")["refusal"] is None


# ─── Configuration embarquée (issue #107) ───────────────────────────────────

def _template():
    return {
        "configVersion": 1,
        "default": {"keycloakClientId": "ne-pas-toucher"},
        "prod": {"keycloakClientId": "client-du-build", "llm_base_urls": "${{LLM_BASE_URL}}",
                 "llm_api_tokens": "jeton-en-dur", "keycloakRealm": "royaume-du-build", "prompt": "métier"},
        "local": {"keycloakClientId": "local"},
    }


def test_governed_values_are_replaced_by_the_instance_placeholders(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "client-de-l-instance")
    monkeypatch.setenv("LLM_API_TOKEN", "jeton-de-l-instance")
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    template, report = package_config.adapt(_template())
    prod = template["prod"]
    assert prod["keycloakClientId"] == "${{KEYCLOAK_CLIENT_ID}}" and prod["llm_api_tokens"] == "${{LLM_API_TOKEN}}"
    assert prod["keycloakRealm"] == "royaume-du-build", "le DM n'a rien à y mettre : la remplacer viderait le réglage"
    assert prod["prompt"] == "métier" and prod["keycloakIssuerUrl"] == "${{KEYCLOAK_ISSUER_URL}}"
    assert template["default"]["keycloakClientId"] == "ne-pas-toucher" and template["local"]["keycloakClientId"] == "local"
    states = {e["key"]: e for e in report}
    assert states["keycloakClientId"]["state"] == package_config.REPLACED
    assert states["keycloakClientId"]["package_value"] == "client-du-build"
    assert states["llm_api_tokens"]["package_value"] == "(valeur masquée)", "un jeton ne s'affiche jamais"
    assert states["keycloakRealm"]["state"] == package_config.KEPT_UNKNOWN
    assert states["llm_base_urls"]["state"] == package_config.PLACEHOLDER
    assert states["keycloakIssuerUrl"]["state"] == package_config.COMPLETED
    digest = package_config.summary(report)
    assert digest["replaced"] == ["prod.llm_api_tokens", "prod.keycloakClientId"] or \
        set(digest["replaced"]) == {"prod.llm_api_tokens", "prod.keycloakClientId"}
    assert digest["kept_unknown"] == ["prod.keycloakRealm"] and len(digest["messages"]) == 2


def test_an_identical_hard_value_is_replaced_without_being_reported_as_a_change(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "client-du-build")
    template, report = package_config.adapt(_template())
    assert template["prod"]["keycloakClientId"] == "${{KEYCLOAK_CLIENT_ID}}"
    assert next(e for e in report if e["key"] == "keycloakClientId")["state"] == package_config.IDENTICAL


def test_declining_the_adaptation_keeps_the_package_values_and_says_so(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "client-de-l-instance")
    template, report = package_config.adapt(_template(), replace=False)
    assert template["prod"]["keycloakClientId"] == "client-du-build"
    assert template["prod"]["keycloakIssuerUrl"] == "${{KEYCLOAK_ISSUER_URL}}", "les clés absentes restent complétées"
    assert "prod.keycloakClientId" in package_config.summary(report)["kept"]


# ─── Constats de relecture : archives ambiguës, chemins sans nom de plugin ───

def _raw_zip(entries: list[tuple[str, str]]) -> bytes:
    import warnings
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries:
                zf.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), content)
    return buf.getvalue()


_PREFS = ("defaults/preferences/prefs.js", "pref('extensions.IAssistant.bootstrapUrls', 'https://dm.example.invalid');\n")
_RDF = ("install.rdf", "<RDF><Description><em:id>p@x</em:id><em:version>1.0.0</em:version></Description></RDF>")


def test_require_cannot_be_bypassed_by_an_archive_that_reads_badly(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    stored = io.BytesIO()
    with zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("install.rdf", (2026, 1, 1, 0, 0, 0)), _RDF[1])
    data = stored.getvalue().replace(b"<em:version>1.0.0", b"<em:version>1.0.9")   # contenu changé, CRC inchangé
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(data)).read("install.rdf")
    # Sans signature : refus de signature, pas une sortie par l'erreur de lecture.
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="plugin", version="1.0.0")
    assert e.value.code == package_import.SIGNATURE_REQUIRED
    # Signée par la chaîne de build mais illisible : refusée aussi, jamais stockée telle quelle.
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="plugin", version="1.0.0",
                                      build_signature=build_envelope(publisher, data))
    assert e.value.code == package_import.UNREADABLE
    assert package_import.preview(data, slug="plugin", version="1.0.0")["refusal"]["code"] == \
        package_import.SIGNATURE_REQUIRED


def test_without_any_setting_an_unreadable_archive_is_still_left_to_the_caller():
    with pytest.raises(zipfile.BadZipFile):
        package_import.import_package(b"pas une archive", version="1.0.0")


def test_require_refuses_a_path_that_does_not_name_the_plugin(monkeypatch, publisher):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    data = xpi("1.0.0")
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, version="1.0.0", build_signature=build_envelope(publisher, data))
    assert e.value.code == package_import.PLUGIN_REQUIRED


def test_unreadable_publisher_keys_refuse_the_import_with_a_message(monkeypatch):
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "warn")
    monkeypatch.setenv("DM_PACKAGE_PUBLISHER_KEYS", "-----BEGIN PUBLIC KEY-----\nnon\n-----END PUBLIC KEY-----")
    data = xpi("1.0.0")
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="plugin", version="1.0.0",
                                      build_signature=build_envelope(new_key(), data))
    assert e.value.code == package_import.SIGNING_CONFIG


@pytest.mark.parametrize("names", [
    ["dm-trust.json", "dm-trust.json"],
    ["DM-TRUST.JSON"],
    ["dm-trust.json", "Dm-Trust.json"],
])
def test_no_anchor_from_the_package_survives_under_any_spelling(signing, names):
    intruder = json.dumps({"schema": "dm-trust/1", "roots": [{"keyid": "intrus"}]})
    data = _raw_zip([_RDF, _PREFS] + [(n, intruder) for n in names])
    if len({n.lower() for n in names}) < len(names):
        # Deux entrées de même nom : l'archive est ambiguë, elle est refusée.
        with pytest.raises(package_import.PackageRefused) as e:
            package_import.import_package(data, slug="p", version="1.0.0")
        assert e.value.code == package_import.UNREADABLE
        return
    served, info = package_import.import_package(data, slug="p", version="1.0.0")
    with zipfile.ZipFile(io.BytesIO(served)) as zf:
        anchors = [n for n in zf.namelist() if n.lower() == "dm-trust.json"]
        assert anchors == ["dm-trust.json"]
        assert json.loads(zf.read("dm-trust.json"))["roots"][0]["keyid"] == ps.key_id(signing.public_key())
    assert info["trust_anchor"] is True


def test_rebuild_drops_every_entry_named_like_a_dm_file():
    data = _raw_zip([_RDF, _PREFS, ("dm-trust.json", "un"), ("dm-trust.json", "deux")])
    served, _info = package_bootstrap.prepare(data, extra_files={"dm-trust.json": b"celui du DM"})
    with zipfile.ZipFile(io.BytesIO(served)) as zf:
        assert [n for n in zf.namelist() if n.lower() == "dm-trust.json"] == ["dm-trust.json"]
        assert zf.read("dm-trust.json") == b"celui du DM"


def test_an_embedded_signature_with_its_own_anchor_is_refused(signing):
    data = _raw_zip([_RDF, ("defaults/preferences/prefs.js", f"pref('extensions.IAssistant.bootstrapUrls', '{HERE}');\n"),
                     ("META-INF/mozilla.rsa", "signature"), ("dm-trust.json", "{}")])
    with pytest.raises(package_import.PackageRefused) as e:
        package_import.import_package(data, slug="p", version="1.0.0")
    assert e.value.code == package_import.FOREIGN_ANCHOR


def test_a_host_that_only_takes_numeric_versions_is_noted_not_refused():
    def crx(manifest):
        return _raw_zip([("manifest.json", json.dumps(manifest))])
    # Extension de navigateur : `version` numérique, numéro complet dans version_name.
    _s, info = package_import.import_package(crx({"version": "1.2.0", "version_name": "1.2.0-rc.3"}), version="1.2.0-rc.3")
    assert info["versions"] == {"manifest.json": "1.2.0-rc.3"} and not any("cœur" in n for n in info["notes"])
    # Sans version_name : le cœur seul est accepté, avec une remarque.
    _s, info = package_import.import_package(crx({"version": "1.2.0"}), version="1.2.0-rc.3")
    assert any("cœur" in n for n in info["notes"])
    # Un autre numéro reste refusé, et Thunderbird (install.rdf) porte toujours le numéro complet.
    with pytest.raises(package_import.PackageRefused):
        package_import.import_package(crx({"version": "1.1.0"}), version="1.2.0-rc.3")
    with pytest.raises(package_import.PackageRefused):
        package_import.import_package(xpi("1.2.0"), version="1.2.0-rc.3")


# ─── LibreOffice : écriture numérique d'une pré-version ──────────────────────

@pytest.mark.parametrize("version,numeric", [
    ("0.4.0", "0.4.0"), ("0.4.0-rc.12", "0.3.99999.2.12"), ("0.4.0-dev.7", "0.3.99999.1.7"),
    ("0.3.1-rc.5", "0.3.0.2.5"), ("1.0.0-rc.1", "0.99999.99999.2.1"),
    ("0.0.0-rc.1", None), ("0.4.0-test3", None), ("0.23.0.154", None), ("", None),
])
def test_libreoffice_numeric_version(version, numeric):
    from app.services.versions import libreoffice_numeric_version
    assert libreoffice_numeric_version(version) == numeric


def test_libreoffice_numeric_versions_sort_like_the_versions_they_stand_for():
    from app.services.versions import libreoffice_numeric_version, version_sort_key
    ordered = ["0.3.0", "0.3.1", "0.4.0-dev.7", "0.4.0-dev.30", "0.4.0-rc.12", "0.4.0", "0.4.1-rc.1", "0.4.1"]
    assert sorted(ordered, key=version_sort_key) == ordered
    numeric = [tuple(int(p) for p in libreoffice_numeric_version(v).split(".")) for v in ordered]
    assert sorted(numeric) == numeric, "l'ordre numérique que voit LibreOffice est celui des versions"


def test_an_oxt_prerelease_may_carry_its_numeric_form_in_description_xml():
    def oxt(native):
        return _raw_zip([("description.xml", f'<description><identifier value="lo.id"/><version value="{native}"/></description>'),
                         ("config.default.json", json.dumps({"enabled": True, "bootstrap_urls": [HERE]}))])
    _s, info = package_import.import_package(oxt("0.3.99999.2.12"), slug="lo", version="0.4.0-rc.12")
    assert info["versions"] == {"description.xml": "0.3.99999.2.12"} and info["nature"] == "rc"
    with pytest.raises(package_import.PackageRefused):
        package_import.import_package(oxt("0.3.99999.2.11"), slug="lo", version="0.4.0-rc.12")
