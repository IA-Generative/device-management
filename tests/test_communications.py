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
