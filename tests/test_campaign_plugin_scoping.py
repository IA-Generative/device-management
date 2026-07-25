"""
Non-régression issue #14 — la campagne de mise à jour doit être scopée au
plugin du device demandeur.

Bug d'origine : `_resolve_active_campaign` retournait la campagne
`plugin_update` active la plus récente TOUS plugins confondus ; un device
LibreOffice recevait la version de la campagne Matisse (0.13.1).

Les interactions DB sont mockées (pas de PostgreSQL réel) : on vérifie que la
requête campagne porte bien le filtre plugin et que `plugin_id`/`device_type`
sont transmis depuis l'endpoint /config jusqu'à la requête.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Bootstrap (même approche que test_enriched_config.py)
# ---------------------------------------------------------------------------

def _setup_env() -> None:
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_RELAY_ENABLED"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_RELAY_REQUIRE_KEY_FOR_SECRETS"] = "false"
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"


def _load_module():
    _setup_env()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = MagicMock()
    fake_psycopg2.Error = Exception
    sys.modules["psycopg2"] = fake_psycopg2

    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    mod.psycopg2 = fake_psycopg2
    return mod


# Ligne campagne, colonnes du SELECT de _resolve_active_campaign :
# camp_id, urgency, deadline_at, target_cohort_id,
# artifact_version, artifact_s3_path, artifact_checksum, changelog_url,
# min_host_version, max_host_version,
# rollback_s3_path, rollback_version, rollback_checksum,
# rollout_config, campaign_created_at, is_experiment
CAMPAIGN_ROW = (
    42, "normal", None, None,
    "2.0.0", "libreoffice/plugin-2.0.0.oxt", "sha256:abc123", None,
    None, None,
    None, None, None,
    None, None, False,
)


def _make_recording_cursor(cursor_rows_by_query: dict) -> MagicMock:
    """Cursor mock façon test_enriched_config, mais qui enregistre (sql, params)."""
    cur = MagicMock()
    cur.calls = []
    _last_sql: list[str] = [""]

    def _execute(sql, params=None):
        _last_sql[0] = sql
        cur.calls.append((sql, params))

    def _fetchall():
        sql = _last_sql[0]
        for fragment, rows in cursor_rows_by_query.items():
            if fragment in sql:
                return list(rows)
        return []

    def _fetchone():
        rows = _fetchall()
        return rows[0] if rows else None

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _campaign_call(cur):
    calls = [(sql, params) for sql, params in cur.calls if "FROM campaigns c" in sql]
    assert calls, f"Requête campagne jamais exécutée. SQL vus: {[sql[:60] for sql, _ in cur.calls]}"
    return calls[-1]


# ---------------------------------------------------------------------------
# Unitaires : _resolve_active_campaign
# ---------------------------------------------------------------------------

def test_campaign_query_filters_by_plugin_id():
    mod = _load_module()
    cur = _make_recording_cursor({"FROM campaigns c": [CAMPAIGN_ROW]})

    campaign = mod._resolve_active_campaign(
        cur,
        device_cohort_ids=[],
        device_type="libreoffice",
        platform_version="",
        plugin_id=7,
    )

    assert campaign is not None
    sql, params = _campaign_call(cur)
    # Le filtre plugin doit exister dans la requête…
    assert "c.plugin_id" in sql, "Le WHERE ne filtre plus par plugin (régression issue #14)"
    assert "a.device_type" in sql, "Pas de fallback device_type pour les campagnes legacy"
    # …et être alimenté par les bons paramètres.
    assert params["plugin_id"] == 7
    assert params["device_type"] == "libreoffice"


def test_campaign_query_unresolved_plugin_passes_null_id():
    """Device non résolu en base (plugin_id None) : le filtre retombe sur
    device_type — on vérifie que None est bien transmis (le SQL fait le reste)."""
    mod = _load_module()
    cur = _make_recording_cursor({"FROM campaigns c": []})

    campaign = mod._resolve_active_campaign(
        cur,
        device_cohort_ids=[1, 2],
        device_type="libreoffice",
        platform_version="",
        plugin_id=None,
    )

    assert campaign is None
    _, params = _campaign_call(cur)
    assert params["plugin_id"] is None
    assert params["device_type"] == "libreoffice"
    assert params["cohort_ids"] == [1, 2]


# ---------------------------------------------------------------------------
# Unitaire : la directive expose l'identité du plugin (défense en profondeur
# côté client — il peut rejeter une directive d'un autre plugin).
# ---------------------------------------------------------------------------

def test_update_directive_includes_plugin_slug():
    mod = _load_module()
    campaign = {
        "campaign_id": 42,
        "urgency": "normal",
        "deadline_iso": None,
        "artifact_version": "2.0.0",
        "artifact_s3_path": "libreoffice/plugin-2.0.0.oxt",
        "artifact_checksum": "sha256:abc123",
        "changelog_url": None,
        "rollback_s3_path": None,
        "rollback_version": None,
        "rollback_checksum": None,
        "rollout_config": None,
    }
    directive = mod._build_update_directive(
        plugin_version="1.0.0",
        campaign=campaign,
        client_uuid="uuid-1",
        device_name="mirai-libreoffice",
    )
    assert directive is not None
    assert directive["action"] == "update"
    assert directive["plugin_slug"] == "mirai-libreoffice"


# ---------------------------------------------------------------------------
# Bout en bout (endpoint /config) : plugin_id résolu → transmis à la requête
# campagne. C'est le chaînon qui manquait (device_type était accepté puis
# ignoré, plugin_id jamais transmis).
# ---------------------------------------------------------------------------

def test_config_endpoint_threads_plugin_id_to_campaign_query():
    mod = _load_module()

    db_rows = {
        "cohorts": [],
        "feature_flags": [],
        "feature_flag_overrides": [],
        "FROM campaigns c": [CAMPAIGN_ROW],
        "campaign_device_status": [],
        # _resolve_device : slug, device_type, id
        "AND status": [("mirai-libreoffice", "libreoffice", 7)],
        # _load_config_template
        "OR device_type": [({"configVersion": 1, "default": {"enabled": True}},)],
    }
    cur = _make_recording_cursor(db_rows)
    conn = MagicMock()
    conn.autocommit = True
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)

    patcher = patch.object(mod.psycopg2, "connect", return_value=conn)
    patcher.start()
    try:
        client = TestClient(mod.app)
        res = client.get(
            "/config/mirai-libreoffice/config.json?profile=prod",
            headers={
                "X-Plugin-Version": "1.0.0",
                "X-Client-UUID": "test-uuid-1234",
                "X-Platform-Type": "libreoffice",
            },
        )
        assert res.status_code == 200
        body = res.json()
        upd = body.get("update")
        assert upd is not None
        assert upd["target_version"] == "2.0.0"
        assert upd["plugin_slug"] == "mirai-libreoffice"

        _, params = _campaign_call(cur)
        assert params["plugin_id"] == 7, (
            "plugin_id résolu non transmis à la requête campagne — "
            "un device peut recevoir la campagne d'un autre plugin (issue #14)"
        )
        assert params["device_type"] == "libreoffice"
    finally:
        patcher.stop()
