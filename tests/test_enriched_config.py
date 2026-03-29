"""
Tests for the EnrichedConfigResponse (schema_version 2) produced by GET /config/config.json
and GET /config/{device}/config.json.

All DB interactions are mocked — no real PostgreSQL is required.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Module bootstrap helpers
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
    # Provide a DB URL so the code attempts DB calls (we will mock psycopg2)
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"


def _make_fake_psycopg2_module():
    """Create a fake psycopg2 module object so patch.object works even without the real package."""
    mod = types.ModuleType("psycopg2")
    mod.connect = MagicMock()  # placeholder — will be overridden per test
    # Add a minimal errors sub-module (some code may reference psycopg2.Error)
    mod.Error = Exception
    return mod


def _load_module():
    _setup_env()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)

    # Always inject a fake psycopg2 so the DB path is exercised in tests
    fake_psycopg2 = _make_fake_psycopg2_module()
    sys.modules["psycopg2"] = fake_psycopg2

    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    # Ensure mod.psycopg2 points to our fake module
    mod.psycopg2 = fake_psycopg2
    return mod


def _make_cursor_mock(cursor_rows_by_query: dict) -> MagicMock:
    """Build a cursor mock whose fetchall/fetchone respond based on the last executed SQL."""
    cur = MagicMock()
    _last_sql: list[str] = [""]

    def _execute(sql, params=None):
        _last_sql[0] = sql

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


def _make_conn_mock(cursor_rows_by_query: dict) -> MagicMock:
    conn = MagicMock()
    conn.autocommit = True
    cur = _make_cursor_mock(cursor_rows_by_query)
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.close = MagicMock()
    return conn


def _install_db_mock(mod, cursor_rows_by_query: dict | None = None):
    """
    Patch mod.psycopg2.connect so it returns a mock conn with the given rows,
    or raises an exception if cursor_rows_by_query is None.

    Returns the patcher (call .stop() in teardown).
    """
    if cursor_rows_by_query is None:
        patcher = patch.object(
            mod.psycopg2, "connect", side_effect=Exception("DB unavailable")
        )
    else:
        conn_mock = _make_conn_mock(cursor_rows_by_query)
        patcher = patch.object(
            mod.psycopg2, "connect", return_value=conn_mock
        )
    patcher.start()
    return patcher


# ---------------------------------------------------------------------------
# Test 1: No X-Plugin-Version → update must be null
# ---------------------------------------------------------------------------

def test_no_plugin_version_returns_null_update():
    mod = _load_module()
    db_rows: dict = {
        "cohorts": [],
        "feature_flags": [],
        "campaigns": [],
    }
    patcher = _install_db_mock(mod, db_rows)
    try:
        client = TestClient(mod.app)
        res = client.get("/config/config.json?profile=prod")
        assert res.status_code == 200
        body = res.json()
        assert body["update"] is None
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# Test 2: schema_version 2 must be present in meta
# ---------------------------------------------------------------------------

def test_schema_version_2_present():
    mod = _load_module()
    db_rows: dict = {}
    patcher = _install_db_mock(mod, db_rows)
    try:
        client = TestClient(mod.app)
        res = client.get("/config/config.json?profile=prod")
        assert res.status_code == 200
        body = res.json()
        assert "meta" in body, f"'meta' key missing from response: {list(body.keys())}"
        assert body["meta"]["schema_version"] == 2
        assert "generated_at" in body["meta"]
        assert "config" in body
        assert "update" in body
        assert "features" in body
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# Test 3: Feature flag with default_value=True appears in features dict
# ---------------------------------------------------------------------------

def test_feature_flag_default_value():
    mod = _load_module()
    db_rows = {
        "cohorts": [],
        # feature_flags query returns (name, default_value)
        "feature_flags": [("my_feature", True)],
        "feature_flag_overrides": [],
        "campaigns": [],
    }
    patcher = _install_db_mock(mod, db_rows)
    try:
        client = TestClient(mod.app)
        res = client.get("/config/config.json?profile=prod")
        assert res.status_code == 200
        body = res.json()
        assert "features" in body
        assert body["features"].get("my_feature") is True
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# Test 4: Plugin v1.0.0 behind artifact v2.0.0 → action = "update"
# ---------------------------------------------------------------------------

def test_update_action_when_behind():
    mod = _load_module()

    # Campaign row columns match _resolve_active_campaign SELECT:
    # camp_id, urgency, deadline_at, target_cohort_id,
    # artifact_version, artifact_s3_path, artifact_checksum, changelog_url,
    # min_host_version, max_host_version,
    # rollback_s3_path, rollback_version, rollback_checksum
    campaign_row = (
        42, "normal", None, None,
        "2.0.0", "libreoffice/plugin-2.0.0.xpi", "sha256:abc123", None,
        None, None,
        None, None, None,
        None, None,  # rollout_config, campaign_created_at
    )

    # Minimal config_template for the plugin (loaded from DB)
    _config_template = {
        "configVersion": 1,
        "default": {"enabled": True},
    }

    db_rows = {
        "cohorts": [],
        "feature_flags": [],
        "feature_flag_overrides": [],
        "campaigns": [campaign_row],
        "campaign_device_status": [],
        # _resolve_device: SELECT slug, device_type, id FROM plugins WHERE slug = %s AND status
        "AND status": [("libreoffice", "libreoffice", 1)],
        # _load_config_template: SELECT config_template FROM plugins WHERE slug = %s OR device_type
        "OR device_type": [(_config_template,)],
    }
    patcher = _install_db_mock(mod, db_rows)
    try:
        client = TestClient(mod.app)
        res = client.get(
            "/config/libreoffice/config.json?profile=prod",
            headers={
                "X-Plugin-Version": "1.0.0",
                "X-Client-UUID": "test-uuid-1234",
            },
        )
        assert res.status_code == 200
        body = res.json()
        upd = body.get("update")
        assert upd is not None, f"Expected update directive, got None. Body keys: {list(body.keys())}"
        assert upd["action"] == "update"
        assert upd["current_version"] == "1.0.0"
        assert upd["target_version"] == "2.0.0"
        assert "/catalog/" in upd["artifact_url"] or "/binaries/" in upd["artifact_url"]
        assert upd["campaign_id"] == 42
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# Test 5: Plugin v2.0.0 matches artifact v2.0.0 → update is null
# ---------------------------------------------------------------------------

def test_update_null_when_current():
    mod = _load_module()

    campaign_row = (
        42, "normal", None, None,
        "2.0.0", "libreoffice/plugin-2.0.0.xpi", "sha256:abc123", None,
        None, None,
        None, None, None,
        None, None,  # rollout_config, campaign_created_at
    )

    db_rows = {
        "cohorts": [],
        "feature_flags": [],
        "feature_flag_overrides": [],
        "campaigns": [campaign_row],
    }
    patcher = _install_db_mock(mod, db_rows)
    try:
        client = TestClient(mod.app)
        res = client.get(
            "/config/config.json?profile=prod",
            headers={
                "X-Plugin-Version": "2.0.0",
                "X-Client-UUID": "test-uuid-1234",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body.get("update") is None, f"Expected null update, got: {body.get('update')}"
    finally:
        patcher.stop()
