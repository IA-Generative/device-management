"""
Tests de la refonte feature-flags v2 (résolution côté serveur).

Hiérarchie : défaut (template.default) → profil (template.<profil>, deep-merge)
→ cohorte (feature_flag_overrides, gating min_plugin_version). Le /config
transporte l'objet RÉSOLU dans `features` ; le catalogue `feature_flags.default_value`
est indicatif et ne participe PAS à la résolution.

Toutes les interactions DB sont mockées — pas de PostgreSQL requis
(réutilise les helpers de test_enriched_config.py).
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from test_enriched_config import _install_db_mock, _load_module

# ---------------------------------------------------------------------------
# Unit: _build_config_from_template — deep-merge featureToggles
# ---------------------------------------------------------------------------

_TEMPLATE = {
    "configVersion": 3,
    "default": {
        "enabled": True,
        "featureToggles": {"composePromptPanel": True, "dailySummary": True, "search": False},
    },
    "int": {"featureToggles": {"search": True}},
    "prod": {},
}


def test_template_deep_merge_feature_toggles():
    """Le profil qui surcharge UN flag ne doit pas effacer les autres (deep vs superficiel)."""
    mod = _load_module()
    out = mod._build_config_from_template(_TEMPLATE, "int")
    assert out["config"]["featureToggles"] == {
        "composePromptPanel": True,   # préservé du default (perdu avec un merge superficiel)
        "dailySummary": True,          # préservé du default
        "search": True,                # surchargé par le profil int
    }


def test_template_profile_without_toggles_keeps_default():
    mod = _load_module()
    out = mod._build_config_from_template(_TEMPLATE, "prod")
    assert out["config"]["featureToggles"] == _TEMPLATE["default"]["featureToggles"]


def test_template_default_without_toggles():
    mod = _load_module()
    tpl = {"default": {"enabled": True}, "int": {"featureToggles": {"search": True}}}
    out = mod._build_config_from_template(tpl, "int")
    assert out["config"]["featureToggles"] == {"search": True}


# ---------------------------------------------------------------------------
# Unit: _resolve_feature_flags — overrides cohorte UNIQUEMENT
# ---------------------------------------------------------------------------

class _FakeCur:
    """Cursor minimal : renvoie les mêmes rows pour tout fetchall."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return list(self._rows)


def test_resolve_flags_no_cohort_returns_empty_without_query():
    mod = _load_module()
    cur = _FakeCur([("search", True, None)])
    assert mod._resolve_feature_flags(cur, device_cohort_ids=[], plugin_version="1.0.0") == {}
    assert cur.executed == [], "sans cohorte, aucun SQL ne doit partir"


def test_resolve_flags_returns_only_cohort_overrides():
    mod = _load_module()
    cur = _FakeCur([("search", True, None)])
    flags = mod._resolve_feature_flags(cur, device_cohort_ids=[1], plugin_version="1.0.0")
    assert flags == {"search": True}


def test_resolve_flags_false_wins_between_cohorts():
    mod = _load_module()
    cur = _FakeCur([("search", True, None), ("search", False, None)])
    flags = mod._resolve_feature_flags(cur, device_cohort_ids=[1, 2], plugin_version="1.0.0")
    assert flags == {"search": False}


def test_resolve_flags_min_plugin_version_gating():
    mod = _load_module()
    rows = [("search", True, "0.13.7")]
    # plugin trop vieux → override ignoré
    assert mod._resolve_feature_flags(_FakeCur(rows), device_cohort_ids=[1], plugin_version="0.13.6") == {}
    # plugin au niveau → override appliqué
    assert mod._resolve_feature_flags(_FakeCur(rows), device_cohort_ids=[1], plugin_version="0.13.7") == {"search": True}
    # version inconnue → fail-safe, override gated ignoré
    assert mod._resolve_feature_flags(_FakeCur(rows), device_cohort_ids=[1], plugin_version="") == {}


# ---------------------------------------------------------------------------
# E2E (/config, DB mockée) : per-profil, override cohorte, gating
# ---------------------------------------------------------------------------

def _db_rows_for_device(extra: dict | None = None) -> dict:
    """Rows mockées pour un device 'matisse' avec _TEMPLATE en DB.

    NB fragments : la requête config_template contient à la fois
    'OR device_type' et 'AND status' — 'OR device_type' doit être déclaré
    AVANT pour matcher en premier (ordre d'insertion du dict).
    """
    rows = {
        "OR device_type": [(_TEMPLATE,)],
        "AND status": [("matisse", "thunderbird", 1)],
        "access_mode": [(1, "open", None)],
        "FROM cohorts": [],
        "cohort_members": [],
        "feature_flag_overrides": [],
        "campaigns": [],
    }
    rows.update(extra or {})
    return rows


def test_config_features_per_profile():
    """int → search=true, prod → search=false : le per-profil vient du template."""
    mod = _load_module()
    patcher = _install_db_mock(mod, _db_rows_for_device())
    try:
        client = TestClient(mod.app)

        res_int = client.get("/config/config.json?profile=int&device=matisse")
        assert res_int.status_code == 200
        feats_int = res_int.json()["features"]
        assert feats_int["search"] is True
        assert feats_int["composePromptPanel"] is True, "deep-merge : flag du default préservé"
        assert feats_int["dailySummary"] is True

        res_prod = client.get("/config/config.json?profile=prod&device=matisse")
        assert res_prod.status_code == 200
        feats_prod = res_prod.json()["features"]
        assert feats_prod["search"] is False
        assert feats_prod["composePromptPanel"] is True
    finally:
        patcher.stop()


def test_config_cohort_override_beats_template():
    """L'override cohorte gagne sur la valeur du profil (int: search=true → false)."""
    mod = _load_module()
    rows = _db_rows_for_device({
        "FROM cohorts": [(7, "manual", {})],
        "cohort_members": [(7,)],
        "feature_flag_overrides": [("search", False, None)],
    })
    patcher = _install_db_mock(mod, rows)
    try:
        client = TestClient(mod.app)
        res = client.get(
            "/config/config.json?profile=int&device=matisse",
            headers={"X-User-Email": "user@example.gouv.fr"},
        )
        assert res.status_code == 200
        feats = res.json()["features"]
        assert feats["search"] is False, "cohorte l'emporte sur le template"
        assert feats["composePromptPanel"] is True, "flags non surchargés inchangés"
    finally:
        patcher.stop()


def test_config_cohort_override_gated_by_min_plugin_version():
    """Override gated : plugin trop vieux → la valeur du profil s'applique."""
    mod = _load_module()
    rows = _db_rows_for_device({
        "FROM cohorts": [(7, "manual", {})],
        "cohort_members": [(7,)],
        "feature_flag_overrides": [("search", False, "9.9.9")],
    })
    patcher = _install_db_mock(mod, rows)
    try:
        client = TestClient(mod.app)
        res = client.get(
            "/config/config.json?profile=int&device=matisse",
            headers={"X-User-Email": "user@example.gouv.fr", "X-Plugin-Version": "0.13.7"},
        )
        assert res.status_code == 200
        assert res.json()["features"]["search"] is True, "override 9.9.9 ignoré pour un plugin 0.13.7"
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# Unit: catalogue scopé — réconciliation à l'import + delete_flag (Phase 3)
# ---------------------------------------------------------------------------

class _ScriptedCur:
    """Cursor scripté : fetchall/fetchone selon un fragment SQL, executes enregistrés."""

    def __init__(self, rows_by_fragment: dict):
        self._rows = rows_by_fragment
        self.executed: list[tuple[str, object]] = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        for fragment, rows in self._rows.items():
            if fragment in self._last_sql:
                return list(rows)
        return []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


def _flags_svc():
    from app.admin.services import flags
    return flags


def test_reconcile_import_adds_union_of_profiles():
    """+flag au bump : union des clés featureToggles (default + tous profils) → UPSERT scopé."""
    svc = _flags_svc()
    cur = _ScriptedCur({"SELECT name, deprecated": []})
    tpl = {
        "configVersion": 3,
        "default": {"featureToggles": {"a": True, "b": False}},
        "int": {"featureToggles": {"c": True}},
        "prod": {},
    }
    diff = svc.reconcile_catalog_from_template(cur, plugin_slug="matisse", template=tpl)
    assert diff["added"] == ["a", "b", "c"]
    assert diff["kept"] == [] and diff["orphaned"] == []
    inserts = [(sql, p) for sql, p in cur.executed if "INSERT INTO feature_flags" in sql]
    assert [(p[0], p[1], p[3]) for _, p in inserts] == [
        ("a", "matisse", True),   # default_value indicatif = template.default
        ("b", "matisse", False),
        ("c", "matisse", True),   # déclaré seulement par le profil int
    ]
    assert all("ON CONFLICT (plugin_slug, name)" in sql for sql, _ in inserts)


def test_reconcile_import_marks_orphans_no_delete():
    """−flag au bump : flag absent du template → deprecated (marqué), jamais DELETE."""
    svc = _flags_svc()
    cur = _ScriptedCur({"SELECT name, deprecated": [("a", False), ("ghost", False), ("dead", True)]})
    tpl = {"default": {"featureToggles": {"a": True}}}
    diff = svc.reconcile_catalog_from_template(cur, plugin_slug="matisse", template=tpl)
    assert diff["kept"] == ["a"]
    assert diff["orphaned"] == ["ghost"]
    assert diff["already_deprecated"] == ["dead"]
    updates = [sql for sql, _ in cur.executed if "SET deprecated = true" in sql]
    assert len(updates) == 1
    assert not any(sql.startswith("DELETE") for sql, _ in cur.executed), "pas d'auto-delete"


def test_reconcile_import_reactivates_returning_flag():
    """Un flag revenu dans le template après avoir été orphelin est réactivé."""
    svc = _flags_svc()
    cur = _ScriptedCur({"SELECT name, deprecated": [("a", True)]})
    tpl = {"default": {"featureToggles": {"a": True}}}
    diff = svc.reconcile_catalog_from_template(cur, plugin_slug="matisse", template=tpl)
    assert diff["reactivated"] == ["a"]
    assert diff["orphaned"] == []


def test_delete_flag_removes_overrides_then_flag():
    svc = _flags_svc()
    cur = _ScriptedCur({"DELETE FROM feature_flags": [(42,)]})
    assert svc.delete_flag(cur, 42) is True
    sqls = [sql for sql, _ in cur.executed]
    assert "DELETE FROM feature_flag_overrides WHERE feature_id = %s" in sqls[0]
    assert "DELETE FROM feature_flags WHERE id = %s RETURNING id" in sqls[1]


def test_delete_flag_missing_returns_false():
    svc = _flags_svc()
    cur = _ScriptedCur({})
    assert svc.delete_flag(cur, 999) is False


def test_resolve_flags_scoped_by_plugin_and_excludes_deprecated():
    """La requête des overrides est scopée (plugin_slug IN ('', slug)) et exclut les deprecated."""
    mod = _load_module()
    cur = _FakeCur([("search", True, None)])
    mod._resolve_feature_flags(cur, device_cohort_ids=[1], plugin_version="1.0.0",
                               plugin_slug="matisse")
    sql = cur.executed[0]
    assert "ff.deprecated = false" in sql
    assert "ff.plugin_slug IN ('', %s)" in sql


def test_config_flag_removed_from_template_disappears():
    """−flag au bump : un flag retiré du template ne doit plus apparaître dans features."""
    mod = _load_module()
    tpl_v2 = {
        "configVersion": 4,
        "default": {"enabled": True, "featureToggles": {"composePromptPanel": True}},
        "int": {},
    }
    patcher = _install_db_mock(mod, _db_rows_for_device({"OR device_type": [(tpl_v2,)]}))
    try:
        client = TestClient(mod.app)
        res = client.get("/config/config.json?profile=int&device=matisse")
        assert res.status_code == 200
        feats = res.json()["features"]
        assert feats == {"composePromptPanel": True}
        assert "search" not in feats, "zéro fantôme côté serveur"
    finally:
        patcher.stop()
