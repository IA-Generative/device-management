"""Liens distribués par /config (clé `links`, issue #83) : résolution par couches
(DM_LINKS ⊕ DM_LINKS_BY_APP[device_type] ⊕ gabarit default ⊕ profil ⊕ catalogue),
fusion PAR id, validation individuelle, liste toujours servie et remplacée en bloc."""
import json

import pytest

import app.runtime_config as rc
from app.main import _apply_catalog_overrides, _apply_overrides, _build_config_from_template
from app.services import links

PORTAL = {
    "id": "search-portal",
    "label": "Chercher aussi dans d'autres sources avec le portail de recherche",
    "url": "https://monportail.numerique-interieur.com/",
    "hint": "en bêta : s'inscrire",
    "hintUrl": "https://communaute.mirai.interieur.gouv.fr/invite/kguG2oQ",
}
SIGNUP = {
    "id": "beta-signup",
    "label": "Rejoindre la communauté bêta",
    "url": "https://communaute.mirai.interieur.gouv.fr/invite/kguG2oQ",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DM_LINKS", "")
    monkeypatch.setenv("DM_LINKS_BY_APP", "")
    rc.apply_state({})
    yield
    rc.apply_state({})


def _cfg(**config):
    return {"config": dict(config)}


# ── service pur ──────────────────────────────────────────────────────────

def test_resolve_valid_entries_kept_in_order():
    out = links.resolve(json.dumps([PORTAL, SIGNUP]))
    assert out == [PORTAL, SIGNUP]


@pytest.mark.parametrize("bad", [
    {"id": "Portal", "label": "x", "url": "https://a"},           # id majuscule
    {"id": "p", "label": "x", "url": "https://a"},                # id trop court
    {"id": "portal", "label": "x", "url": "http://a"},            # http
    {"id": "portal", "label": "x", "url": "https://a/${{VAR}}"},  # gabarit non résolu
    {"id": "portal", "label": "x"},                               # url manquante
    {"id": "portal", "url": "https://a"},                         # label manquant
    "pas-un-objet",
])
def test_invalid_entry_dropped_individually(bad):
    out = links.resolve([bad, SIGNUP])
    assert out == [SIGNUP]


def test_bad_hint_url_dropped_but_link_kept():
    out = links.resolve([{**PORTAL, "hintUrl": "javascript:alert(1)"}])
    assert len(out) == 1 and "hintUrl" not in out[0] and out[0]["hint"] == PORTAL["hint"]


def test_label_truncated_and_whitespace_collapsed():
    out = links.resolve([{**SIGNUP, "label": "  a " * 60}])
    assert out[0]["label"].startswith("a a a") and "  " not in out[0]["label"]
    assert len(out[0]["label"]) <= 80 and not out[0]["label"].endswith(" ")


def test_invalid_json_layer_ignored_not_fatal():
    assert links.resolve("{pas du json", [SIGNUP]) == [SIGNUP]
    assert links.resolve('{"objet": "pas une liste"}') == []


def test_merge_by_id_field_by_field_later_wins():
    out = links.resolve([PORTAL, SIGNUP], [{"id": "search-portal", "url": "https://autre.example/"}])
    assert out[0] == {**PORTAL, "url": "https://autre.example/"}
    assert out[1] == SIGNUP


def test_hidden_removes_and_empty_string_clears_optional_field():
    out = links.resolve([PORTAL, SIGNUP],
                        [{"id": "beta-signup", "hidden": True}, {"id": "search-portal", "hintUrl": ""}])
    assert [e["id"] for e in out] == ["search-portal"]
    assert "hintUrl" not in out[0]


def test_by_app_layer_only_for_matching_device_type():
    by_app = {"pablo": [{"id": "search-portal", "hidden": True}],
              "matisse": [{"id": "help", "label": "Aide", "url": "https://aide.example/"}]}
    assert [e["id"] for e in links.resolve([PORTAL], device_type="matisse", by_app=by_app)] == [
        "search-portal", "help"]
    assert links.resolve([PORTAL], device_type="pablo", by_app=by_app) == []
    assert links.resolve([PORTAL], device_type="libreoffice", by_app=json.dumps(by_app)) == [PORTAL]
    assert links.resolve([PORTAL], device_type=None, by_app=by_app) == [PORTAL]


# ── gabarit dm-config.json ───────────────────────────────────────────────

def test_template_links_merged_by_id_default_then_profile():
    tpl = {"default": {"links": [PORTAL, SIGNUP], "systemPrompt": "x"},
           "int": {"links": [{"id": "search-portal", "url": "https://int.example/"}]}}
    cfg = _build_config_from_template(tpl, "int")
    ids = {e["id"]: e for e in cfg["config"]["links"]}
    assert ids["search-portal"]["url"] == "https://int.example/"
    assert ids["search-portal"]["label"] == PORTAL["label"]  # pas perdu par le profil
    assert "beta-signup" in ids


def test_template_without_links_leaves_key_absent():
    assert "links" not in _build_config_from_template({"default": {"a": 1}}, "prod")["config"]


# ── /config : _apply_overrides ───────────────────────────────────────────

def test_links_always_served_as_list_even_when_empty():
    out = _apply_overrides(_cfg(foo="bar"), profile="prod", device="matisse")
    assert out["config"]["links"] == []


def test_env_common_layer_served(monkeypatch):
    monkeypatch.setenv("DM_LINKS", json.dumps([PORTAL, SIGNUP]))
    out = _apply_overrides(_cfg(), profile="prod", device="matisse")
    assert out["config"]["links"] == [PORTAL, SIGNUP]


def test_admin_override_hot_reloaded(monkeypatch):
    rc.apply_state({"DM_LINKS": json.dumps([SIGNUP])})
    out = _apply_overrides(_cfg(), profile="prod", device="matisse")
    assert out["config"]["links"] == [SIGNUP]


def test_layer_priority_env_then_by_app_then_template(monkeypatch):
    monkeypatch.setenv("DM_LINKS", json.dumps([PORTAL, SIGNUP]))
    monkeypatch.setenv("DM_LINKS_BY_APP", json.dumps({
        "matisse": [{"id": "search-portal", "url": "https://app.example/"},
                    {"id": "beta-signup", "hidden": True}]}))
    template_links = [{"id": "search-portal", "hint": "bientôt public"}]
    out = _apply_overrides(_cfg(links=template_links), profile="prod", device="matisse")
    assert out["config"]["links"] == [
        {**PORTAL, "url": "https://app.example/", "hint": "bientôt public"}]
    # Autre application : la couche par app ne s'applique pas.
    out2 = _apply_overrides(_cfg(links=template_links), profile="prod", device="libreoffice")
    assert [e["id"] for e in out2["config"]["links"]] == ["search-portal", "beta-signup"]


def test_template_garbage_links_do_not_break_config():
    out = _apply_overrides(_cfg(links="n'importe quoi"), profile="prod", device="matisse")
    assert out["config"]["links"] == []


# ── surcharge catalogue (plugin_env_overrides) ───────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self._q = ""

    def execute(self, q, params=None):
        self._q = q

    def fetchall(self):
        return self._rows if "plugin_env_overrides" in self._q else []

    def fetchone(self):
        return None


def test_catalog_override_merges_links_by_id_instead_of_replacing_with_text():
    cur = _Cur([("links", json.dumps([{"id": "search-portal", "url": "https://cat.example/"}])),
                ("autreCle", "valeur")])
    out = _apply_catalog_overrides(_cfg(links=[PORTAL, SIGNUP]), plugin_id=1, profile="prod", cur=cur)
    assert out["config"]["autreCle"] == "valeur"
    assert out["config"]["links"] == [{**PORTAL, "url": "https://cat.example/"}, SIGNUP]


def test_catalog_override_can_hide_a_link():
    cur = _Cur([("links", json.dumps([{"id": "beta-signup", "hidden": True}]))])
    out = _apply_catalog_overrides(_cfg(links=[PORTAL, SIGNUP]), plugin_id=1, profile="prod", cur=cur)
    assert out["config"]["links"] == [PORTAL]


# ── registre admin ───────────────────────────────────────────────────────

def test_runtime_config_json_keys_validated():
    spec = rc.EDITABLE_KEYS["DM_LINKS"]
    assert spec.hot_reloadable and spec.type == "json" and not spec.sensitive
    assert rc.coerce_input(spec, ' [] ') == "[]"
    assert rc.coerce_input(spec, "") == ""
    with pytest.raises(ValueError):
        rc.coerce_input(spec, "[pas du json")
    assert "DM_LINKS_BY_APP" in rc.EDITABLE_KEYS


def test_baseline_tolerates_invalid_env_json(monkeypatch):
    monkeypatch.setenv("DM_LINKS", "{cassé")
    rc.snapshot_baseline(force=True)
    assert rc.env_baseline("DM_LINKS") == ""
    monkeypatch.setenv("DM_LINKS", "")
    rc.snapshot_baseline(force=True)
