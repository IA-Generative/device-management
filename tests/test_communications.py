"""
Communications côté plugin : résolution des annonces actives, exposition dans
GET /config et acquittement POST /communications/{id}/ack.

Toutes les interactions DB sont mockées — pas de PostgreSQL requis (mêmes
helpers que test_enriched_config.py pour l'endpoint).
"""
from __future__ import annotations

import re

from app.admin.services import communications as comms_svc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingCur:
    """Curseur minimal : mémorise (sql, params) et sert des rows fixes."""

    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_params(self):
        return self.calls[-1][1]


# ---------------------------------------------------------------------------
# create_communication — l'INSERT doit correspondre au schéma
# ---------------------------------------------------------------------------


def test_create_communication_insert_matches_schema_columns():
    """La colonne target_bundle_id n'existe dans aucun schéma : l'INSERT ne
    doit pas la nommer, et chaque colonne nommée doit avoir son paramètre."""
    cur = _CapturingCur(one=(42,))
    comm_id = comms_svc.create_communication(
        cur, type="announcement", title="Titre", body="Corps", status="active",
    )
    assert comm_id == 42
    sql, params = cur.last_sql, cur.last_params
    assert "target_bundle_id" not in sql
    columns = re.search(r"INSERT INTO communications\s*\((.*?)\)\s*VALUES", sql, re.S).group(1)
    n_columns = len([c for c in columns.split(",") if c.strip()])
    assert sql.count("%s") == n_columns == len(params)


# ---------------------------------------------------------------------------
# get_active_communications — ciblage plugin / cohorte / versions, projection
# ---------------------------------------------------------------------------

# Ordre des colonnes du SELECT de get_active_communications.
def _row(id=1, type="announcement", title="T", body="B", priority="normal",
         min_pv=None, max_pv=None, sq=None, sc=None, sam=False, sac=False):
    return (id, type, title, body, priority, min_pv, max_pv, sq, sc, sam, sac)


def _active(cur, **kw):
    args = {"plugin_slug": "matisse", "client_uuid": "uuid-1",
            "device_cohort_ids": [], "plugin_version": "1.0.0"}
    args.update(kw)
    return comms_svc.get_active_communications(cur, **args)


def test_get_active_passes_targeting_params():
    cur = _CapturingCur(rows=[])
    _active(cur, plugin_slug="matisse", client_uuid="uuid-9", device_cohort_ids=[3, 5])
    assert "target_cohort_id" in cur.last_sql
    params = list(cur.last_params)
    assert "matisse" in params
    assert "uuid-9" in params
    assert [3, 5] in params


def test_get_active_version_gating_is_inclusive_and_fail_safe():
    rows = [_row(id=1, min_pv="0.18.0"), _row(id=2, max_pv="0.17.9"), _row(id=3)]
    ids = lambda v: [c["id"] for c in _active(_CapturingCur(rows), plugin_version=v)]  # noqa: E731
    assert ids("0.18.0") == [1, 3]      # min inclusif, max dépassé
    assert ids("0.17.9") == [2, 3]      # max inclusif, min non atteint
    assert ids("0.17.2") == [2, 3]
    assert ids("") == [3]               # version inconnue : toute borne exclut


def test_get_active_projection_announcement_and_survey():
    rows = [_row(id=1), _row(id=2, type="survey", sq="Q ?", sc='["a", "b"]', sam=True, sac=False)]
    out = _active(_CapturingCur(rows))
    assert out[0] == {"id": 1, "type": "announcement", "title": "T", "body": "B", "priority": "normal"}
    assert out[1] == {
        "id": 2, "type": "survey", "title": "T", "body": "B", "priority": "normal",
        "survey_question": "Q ?", "survey_choices": ["a", "b"],
        "survey_allow_multiple": True, "survey_allow_comment": False,
    }


def test_get_active_caps_at_ten_after_version_filtering():
    rows = [_row(id=i, min_pv="9.0.0") for i in range(3)] + [_row(id=10 + i) for i in range(12)]
    out = _active(_CapturingCur(rows), plugin_version="1.0.0")
    assert [c["id"] for c in out] == list(range(10, 20))
