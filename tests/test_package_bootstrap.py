"""Liste de DM inscrite dans un paquet de plugin (issue #133).

Un paquet servi par un DM doit ramener ses postes vers ce DM : à l'envoi, la
liste du paquet est lue, comparée à celle de l'instance et réécrite si elle
diffère. Ici la règle seule, format par format, sans base ni route.
"""
from __future__ import annotations

import io
import json
import zipfile
from xml.etree import ElementTree

import pytest

from app.services import package_bootstrap as pb

HERE = "https://dm-ici.example"
ELSEWHERE = "https://dm-ailleurs.example"

PREFS = (
    "// Default preferences\n"
    "pref('extensions.IAssistant.llmModel', '');\n"
    "pref('extensions.IAssistant.bootstrapUrls', '{urls}');\n"
    "pref('extensions.IAssistant.bootstrapUrl', '');\n"
    "pref('extensions.IAssistant.configProfile', 'prod');\n"
)
DESCRIPTION = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<description xmlns="http://openoffice.org/extensions/description/2006"\n'
    '             xmlns:xlink="http://www.w3.org/1999/xlink">\n'
    '  <identifier value="fr.gouv.interieur.mirai"/>\n'
    '  <version value="1.2.3"/>\n'
    "{feed}"
    "</description>\n"
)


def _zip(files: dict, prefix: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), content)
    return prefix + buf.getvalue()


def _read(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name).decode("utf-8")


def _xpi(urls: str, **extra) -> bytes:
    return _zip({"install.rdf": "<RDF/>", "defaults/preferences/prefs.js": PREFS.format(urls=urls), **extra})


def _oxt(config: dict, feed: str = "") -> bytes:
    return _zip({"mimetype": "application/vnd.openofficeorg.extension",
                 "description.xml": DESCRIPTION.format(feed=feed),
                 "config.default.json": json.dumps(config, indent=4)})


def _feed(*urls: str) -> str:
    return ("  <update-information>\n"
            + "".join(f'    <src xlink:href="{u}/catalog/mirai-libreoffice/update.xml"/>\n' for u in urls)
            + "  </update-information>\n")


def _browser(config: dict, **extra) -> bytes:
    return _zip({"manifest.json": '{"manifest_version": 3}',
                 "src/dm/config.json": json.dumps(config, indent=2),
                 "src/dm/bootstrap.js": "globalThis.DMBootstrap = {};", **extra})


# ── Adresses ────────────────────────────────────────────────────────────────

def test_lists_compare_in_order_ignoring_case_and_trailing_slash():
    assert pb.same_list(["https://DM.Example/", "https://b.example"], ["https://dm.example", "https://b.example/"])
    assert not pb.same_list(["https://a.example", "https://b.example"], ["https://b.example", "https://a.example"])
    assert not pb.same_list(["https://a.example/dm"], ["https://a.example/DM"]), "le chemin garde sa casse"


def test_placeholders_and_blanks_are_not_addresses():
    assert pb.clean_urls(["__BOOTSTRAP_URLS__", " ", "${{BOOTSTRAP_URL_1}}", "https://a.example/",
                          "https://A.example"]) == ["https://a.example"]


def test_instance_list_is_dm_bootstrap_urls_then_public_base_url(monkeypatch):
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "https://un.example, https://deux.example/")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public.example/dm/")
    assert pb.instance_urls() == ["https://un.example", "https://deux.example"]
    monkeypatch.setenv("DM_BOOTSTRAP_URLS", "")
    assert pb.instance_urls() == ["https://public.example/dm"]
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    assert pb.instance_urls() == []


# ── Thunderbird ─────────────────────────────────────────────────────────────

def test_thunderbird_list_naming_another_dm_is_rewritten():
    data, info = pb.prepare(_xpi(f"{ELSEWHERE},https://autre.example/"), instance=[HERE, "https://secours.example"])

    assert info["status"] == pb.REWRITTEN and info["format"] == "thunderbird-prefs"
    assert info["package_urls"] == [ELSEWHERE, "https://autre.example"]
    assert info["served_urls"] == [HERE, "https://secours.example"]
    assert ELSEWHERE in info["message"] and HERE in info["message"]
    prefs = _read(data, "defaults/preferences/prefs.js")
    assert f"pref('extensions.IAssistant.bootstrapUrls', '{HERE},https://secours.example');" in prefs
    # Rien d'autre ne bouge : ni le dernier DM gagnant (au singulier), ni les autres réglages.
    assert prefs == PREFS.format(urls=f"{HERE},https://secours.example")
    assert pb.inspect(data)["urls"] == [HERE, "https://secours.example"]


def test_thunderbird_list_already_right_is_left_alone():
    package = _xpi(f"{HERE}/")
    data, info = pb.prepare(package, instance=[HERE])
    assert info["status"] == pb.ALIGNED and info["message"] == ""
    assert _read(data, "defaults/preferences/prefs.js") == _read(package, "defaults/preferences/prefs.js")


def test_thunderbird_example_build_receives_the_instance_list():
    data, info = pb.prepare(_xpi("__BOOTSTRAP_URLS__"), instance=[HERE])
    assert info["status"] == pb.REWRITTEN and info["package_urls"] == []
    assert f"'extensions.IAssistant.bootstrapUrls', '{HERE}'" in _read(data, "defaults/preferences/prefs.js")


def test_same_addresses_in_another_order_are_rewritten():
    data, info = pb.prepare(_xpi(f"{ELSEWHERE},{HERE}"), instance=[HERE, ELSEWHERE])
    assert info["status"] == pb.REWRITTEN, "l'ordre est la priorité"
    assert pb.inspect(data)["urls"] == [HERE, ELSEWHERE]


def test_dm_metadata_is_still_stripped():
    data, _ = pb.prepare(_xpi(HERE, **{"dm-config.json": "{}", "dm-manifest.json": "{}"}), instance=[HERE])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["defaults/preferences/prefs.js", "install.rdf"]


def test_rewriting_is_reproducible():
    package = _xpi(ELSEWHERE)
    assert pb.prepare(package, instance=[HERE])[0] == pb.prepare(package, instance=[HERE])[0]


# ── LibreOffice ─────────────────────────────────────────────────────────────

def test_libreoffice_config_and_native_feed_are_rewritten():
    config = {"configVersion": 1, "enabled": True, "bootstrap_urls": [ELSEWHERE, "https://b.example"],
              "bootstrap_insecure_urls": ["interne.example"], "config_path": "/config/mirai-libreoffice/config.json?profile=prod"}
    data, info = pb.prepare(_oxt(config, _feed(ELSEWHERE, "https://b.example")), instance=[f"{HERE}/dm"])

    assert info["status"] == pb.REWRITTEN and info["format"] == "libreoffice-config"
    stored = json.loads(_read(data, "config.default.json"))
    assert stored == {**config, "bootstrap_urls": [f"{HERE}/dm"]}, "seule la liste change"
    description = _read(data, "description.xml")
    ElementTree.fromstring(description.encode("utf-8"))
    assert f'<src xlink:href="{HERE}/dm/catalog/mirai-libreoffice/update.xml"/>' in description
    assert ELSEWHERE not in description and "b.example" not in description
    assert '<version value="1.2.3"/>' in description
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist()[0] == "mimetype", "l'ordre des entrées de l'OXT est conservé"


def test_libreoffice_single_url_key_follows_when_the_package_sets_it():
    data, info = pb.prepare(_oxt({"enabled": True, "bootstrap_url": ELSEWHERE}), instance=[HERE, "https://b.example"])
    stored = json.loads(_read(data, "config.default.json"))
    assert info["package_urls"] == [ELSEWHERE]
    assert stored["bootstrap_url"] == HERE and stored["bootstrap_urls"] == [HERE, "https://b.example"]


def test_libreoffice_offline_package_is_not_touched():
    package = _oxt({"enabled": False, "bootstrap_urls": [], "llm_base_urls": "http://localhost:11434"})
    data, info = pb.prepare(package, instance=[HERE])
    assert info["status"] == pb.OFFLINE and info["message"] == ""
    assert _read(data, "config.default.json") == _read(package, "config.default.json")


def test_libreoffice_feed_that_does_not_derive_from_the_list_is_kept_and_said():
    feed = '  <update-information>\n    <src xlink:href="https://flux.example/update.xml"/>\n  </update-information>\n'
    data, info = pb.prepare(_oxt({"enabled": True, "bootstrap_urls": [ELSEWHERE]}, feed), instance=[HERE])
    assert info["status"] == pb.REWRITTEN
    assert "https://flux.example/update.xml" in _read(data, "description.xml")
    assert any("flux de mise à jour natif" in note for note in info["notes"])


# ── Extension de navigateur ─────────────────────────────────────────────────

def test_browser_active_profile_is_rewritten_and_only_it():
    config = {"activeProfile": "prod", "default": {"enabled": True},
              "int": {"bootstrap_url": "https://int.example"},
              "prod": {"bootstrap_urls": [ELSEWHERE, "https://b.example"], "keycloakRealm": "mirai"}}
    data, info = pb.prepare(_browser(config), instance=[HERE])

    assert info["status"] == pb.REWRITTEN and info["format"] == "browser-config"
    assert info["package_urls"] == [ELSEWHERE, "https://b.example"]
    stored = json.loads(_read(data, "src/dm/config.json"))
    assert stored["prod"] == {"bootstrap_urls": [HERE], "keycloakRealm": "mirai"}
    assert stored["int"] == config["int"] and stored["default"] == config["default"]
    assert any("adresses de repli" in note for note in info["notes"])


def test_browser_single_url_profile():
    config = {"activeProfile": "int", "int": {"bootstrap_url": ELSEWHERE}}
    data, info = pb.prepare(_browser(config), instance=[HERE])
    assert json.loads(_read(data, "src/dm/config.json"))["int"] == {"bootstrap_url": HERE, "bootstrap_urls": [HERE]}


# ── Paquet signé ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("package", [
    _xpi(ELSEWHERE, **{"META-INF/mozilla.rsa": "sig", "META-INF/manifest.mf": "x"}),
    _xpi(ELSEWHERE, **{"META-INF/cose.sig": "sig"}),
], ids=["xpi-rsa", "xpi-cose"])
def test_signed_package_is_refused_not_rewritten(package):
    with pytest.raises(pb.SignedPackageMismatch) as refused:
        pb.prepare(package, instance=[HERE])
    assert "signé" in str(refused.value) and ELSEWHERE in str(refused.value)

    data, info = pb.prepare(package, instance=[HERE], keep=True)
    assert info["status"] == pb.KEPT and info["served_urls"] == [ELSEWHERE]
    assert pb.inspect(data)["urls"] == [ELSEWHERE], "conservé : jamais réécrit"
    assert ELSEWHERE in info["message"]


def test_signed_package_with_the_right_list_passes():
    _, info = pb.prepare(_xpi(HERE, **{"META-INF/mozilla.rsa": "sig"}), instance=[HERE])
    assert info["status"] == pb.ALIGNED and info["signed"] is True


def test_crx_header_counts_as_a_signature():
    assert pb.is_signed(b"Cr24" + b"\x03\x00\x00\x00")
    assert not pb.is_signed(_oxt({"enabled": True}))
    assert not pb.is_signed(_zip({"META-INF/manifest.xml": "<manifest/>"})), "manifeste OXT, pas une signature"


# ── Cas où l'on ne réécrit pas ──────────────────────────────────────────────

def test_instance_without_known_address_does_not_rewrite_but_says_where_devices_will_go():
    package = _xpi(ELSEWHERE)
    data, info = pb.prepare(package, instance=[])
    assert info["status"] == pb.INSTANCE_UNKNOWN and ELSEWHERE in info["message"]
    assert pb.inspect(data)["urls"] == [ELSEWHERE]


@pytest.mark.parametrize("bad", ["https://dm.example/a'b", 'https://dm.example/"x', "ftp://dm.example",
                                 "https://dm.example/a b", "https://dm.example/\\x"])
def test_instance_address_that_would_escape_its_string_is_never_written(bad):
    data, info = pb.prepare(_xpi(ELSEWHERE), instance=[bad])
    assert info["status"] == pb.INSTANCE_INVALID
    assert pb.inspect(data)["urls"] == [ELSEWHERE]


def test_package_without_dm_list_passes_silently():
    _, info = pb.prepare(_zip({"manifest.json": "{}"}), instance=[HERE])
    assert info["status"] == pb.NONE and info["message"] == "" and info["format"] is None


def test_unreadable_archive_raises_for_prepare_and_is_reported_by_inspect():
    with pytest.raises(zipfile.BadZipFile):
        pb.prepare(b"pas une archive", instance=[HERE])
    assert pb.inspect(b"pas une archive")["status"] == pb.UNREADABLE


# ── Fiche d'une version ─────────────────────────────────────────────────────

def test_describe_flags_a_package_that_no_longer_matches_the_instance():
    _, info = pb.prepare(_xpi(ELSEWHERE), instance=[HERE])
    shown = pb.describe(info, [HERE])
    assert shown["served_urls"] == [HERE] and shown["rewritten"] and not shown["mismatch"]
    assert shown["package_urls"] == [ELSEWHERE]
    # La liste de l'instance a changé depuis l'envoi : l'écart apparaît après coup.
    assert pb.describe(info, ["https://nouveau.example"])["mismatch"]


def test_describe_reads_a_stored_package_sent_before_the_check(tmp_path):
    path = tmp_path / "ancien.xpi"
    path.write_bytes(_xpi(ELSEWHERE))
    shown = pb.describe(pb.inspect_file(str(path)), [HERE])
    assert shown["served_urls"] == [ELSEWHERE] and shown["mismatch"] and not shown["rewritten"]
    assert pb.inspect_file(str(tmp_path / "absent.xpi")) is None
    assert pb.describe(None, [HERE]) is None


def test_describe_package_without_list_has_no_mismatch():
    _, info = pb.prepare(_zip({"manifest.json": "{}"}), instance=[HERE])
    shown = pb.describe(info, [HERE])
    assert not shown["has_list"] and not shown["mismatch"]
