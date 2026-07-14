"""
Tests du lot « dashboard cohérent » :
- token télémétrie : claim `cuid` (identité STABLE) embarqué au mint — le jti
  (uuid4 par token) ne doit plus jamais servir d'identité dans device_connections ;
- helper métriques dashboard : appareils actifs depuis plugin_installations
  (non pollué) + volume d'interactions séparé.

DB mockée — réutilise les helpers de test_enriched_config.py.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

from test_enriched_config import _load_module


def _decode_token_payload(token: str) -> dict:
    payload_b64 = token.split(".", 1)[0]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_mint_token_embeds_stable_client_uuid():
    mod = _load_module()
    with patch.object(mod.settings, "telemetry_token_signing_key", "k-test"):
        token, exp = mod._mint_telemetry_token(device="matisse", profile="int",
                                               client_uuid="5b29896c-0a98-4067-95d8-579d64032adb")
    assert token and exp
    payload = _decode_token_payload(token)
    assert payload["cuid"] == "5b29896c-0a98-4067-95d8-579d64032adb"
    assert payload["jti"] != payload["cuid"], "le jti reste distinct (anti-rejeu), le cuid porte l'identité"


def test_mint_token_without_identity_has_no_cuid():
    mod = _load_module()
    with patch.object(mod.settings, "telemetry_token_signing_key", "k-test"):
        token, _ = mod._mint_telemetry_token(device="matisse", profile="int")
    assert "cuid" not in _decode_token_payload(token)


def test_mint_token_jti_changes_but_cuid_is_stable():
    """Deux tokens du même client : jti différents (rotation), cuid identique."""
    mod = _load_module()
    with patch.object(mod.settings, "telemetry_token_signing_key", "k-test"):
        t1, _ = mod._mint_telemetry_token(device="m", profile="int", client_uuid="u-1")
        t2, _ = mod._mint_telemetry_token(device="m", profile="int", client_uuid="u-1")
    p1, p2 = _decode_token_payload(t1), _decode_token_payload(t2)
    assert p1["jti"] != p2["jti"]
    assert p1["cuid"] == p2["cuid"] == "u-1"


class _ScriptedCur:
    """Cursor scripté : fetchone selon un fragment SQL."""

    def __init__(self, rows_by_fragment: dict):
        self._rows = rows_by_fragment
        self.executed = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.executed.append(self._last)

    def fetchone(self):
        for frag, val in self._rows.items():
            if frag in self._last:
                return (val,)
        return (0,)

    def fetchall(self):
        return []


def test_dashboard_metrics_active_from_installations_not_connections():
    """« Appareils actifs » vient du heartbeat (plugin_installations), les
    interactions du volume brut de device_connections — plus jamais un
    COUNT(DISTINCT client_uuid) sur la table polluée par les jti."""
    from app.admin import router as admin_router
    cur = _ScriptedCur({
        "FROM plugin_installations": 23,
        "FROM device_connections": 1104,
        "provisioning WHERE status = 'ENROLLED'": 23,
        "FROM provisioning": 25,
        "FROM campaigns": 2,
    })
    m = admin_router._compute_dashboard_metrics(cur)
    assert m["active_devices"] == 23
    assert m["interactions_7d"] == 1104
    active_sql = next(s for s in cur.executed if "plugin_installations" in s)
    assert "COUNT(DISTINCT client_uuid)" in active_sql and "last_seen_at" in active_sql
    inter_sql = next(s for s in cur.executed if "device_connections" in s)
    assert "COUNT(*)" in inter_sql, "interactions = volume brut, pas un distinct pollué"
