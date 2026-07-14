"""
Fix long terme colonne Plugin du journal d'audit : plugin_slug PERSISTÉ à
l'écriture par le helper audit_log (dérivation ressource/payload/catalogue),
backfill one-shot à la migration, lecture avec repli.
"""
from __future__ import annotations

from app.admin import helpers


class _ScriptedCur:
    """Cursor scripté : fetchone selon un fragment SQL, executes enregistrés."""

    def __init__(self, rows_by_fragment: dict | None = None):
        self._rows = rows_by_fragment or {}
        self.executed: list[tuple[str, object]] = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.executed.append((self._last, params))

    def fetchone(self):
        for frag, val in self._rows.items():
            if frag in self._last:
                return val
        return None


def _inserted_plugin(cur) -> object:
    sql, params = next((s, p) for s, p in cur.executed if "INSERT INTO admin_audit_log" in s)
    assert "plugin_slug" in sql
    return params[-1]


def test_audit_log_derives_from_plugin_resource():
    cur = _ScriptedCur()
    helpers.audit_log(cur, actor={}, action="flag.reconcile",
                      resource_type="plugin", resource_id="mirai-matisse")
    assert _inserted_plugin(cur) == "mirai-matisse"


def test_audit_log_plugin_resource_numeric_id_resolved_via_catalog():
    """plugin:<id numérique> (actions historiques type plugin.update) → slug."""
    cur = _ScriptedCur({"FROM plugins WHERE id": ("mirai-matisse",)})
    helpers.audit_log(cur, actor={}, action="plugin.update",
                      resource_type="plugin", resource_id="9")
    assert _inserted_plugin(cur) == "mirai-matisse"


def test_audit_log_plugin_resource_star_is_no_plugin():
    """plugin:* (purge globale) → aucun plugin unique → NULL."""
    cur = _ScriptedCur()
    helpers.audit_log(cur, actor={}, action="plugin.purge_removed",
                      resource_type="plugin", resource_id="*",
                      payload={"deleted_count": 1})
    assert _inserted_plugin(cur) is None


def test_audit_log_derives_from_payload_slug():
    cur = _ScriptedCur()
    helpers.audit_log(cur, actor={}, action="flag.create", resource_type="flag",
                      resource_id="17", payload={"plugin_slug": "mirai-matisse"})
    assert _inserted_plugin(cur) == "mirai-matisse"


def test_audit_log_resolves_payload_plugin_id_via_catalog():
    cur = _ScriptedCur({"FROM plugins WHERE id": ("mirai-matisse",)})
    helpers.audit_log(cur, actor={}, action="version.upload",
                      resource_type="plugin_version", resource_id="23",
                      payload={"version": "0.13.8", "plugin_id": 9})
    assert _inserted_plugin(cur) == "mirai-matisse"


def test_audit_log_resolves_flag_via_feature_flags():
    cur = _ScriptedCur({"FROM feature_flags WHERE id": ("mirai-matisse",)})
    helpers.audit_log(cur, actor={}, action="flag.update",
                      resource_type="flag", resource_id="7",
                      payload={"before": True, "after": False})
    assert _inserted_plugin(cur) == "mirai-matisse"


def test_audit_log_explicit_plugin_wins_and_none_when_underivable():
    cur = _ScriptedCur()
    helpers.audit_log(cur, actor={}, action="cohort.create",
                      resource_type="cohort", resource_id="3", plugin="forced-slug")
    assert _inserted_plugin(cur) == "forced-slug"
    cur2 = _ScriptedCur()
    helpers.audit_log(cur2, actor={}, action="cohort.create",
                      resource_type="cohort", resource_id="3")
    assert _inserted_plugin(cur2) is None


def test_audit_log_derivation_failure_never_blocks_insert():
    class _BoomCur(_ScriptedCur):
        def execute(self, sql, params=None):
            if "FROM plugins" in sql:
                raise RuntimeError("db down")
            super().execute(sql, params)

    cur = _BoomCur()
    helpers.audit_log(cur, actor={}, action="deploy.create",
                      resource_type="campaign", resource_id="16",
                      payload={"plugin_id": 9})
    assert _inserted_plugin(cur) is None, "échec de dérivation ⇒ NULL, jamais d'exception"
