"""FK plugin_installations.plugin_id → ON DELETE CASCADE (non-régression).

Bug constaté sur DGX (0.9.9) : la contrainte d'origine était en NO ACTION —
seule FK vers plugins(id) sans CASCADE parmi ses sœurs (plugin_versions,
plugin_aliases, …) — et bloquait la purge des plugins 'removed' dès qu'une
installation les référençait :

    update or delete on table "plugins" violates foreign key constraint
    "plugin_installations_plugin_id_fkey" on table "plugin_installations"

Trois niveaux de garde :
1. unitaires : le schéma source déclare CASCADE, et apply_schema porte le
   fixup de migration (une base existante ne rejoue pas CREATE TABLE) ;
2. comportement : purge_removed vide campaigns puis plugins (ordre préservé) ;
3. intégration (vrai Postgres, DATABASE_URL) : une base « legacy » (FK sans
   CASCADE) migrée par apply_schema purge un plugin avec installations, et la
   migration est rejouable.
"""
import inspect
import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "db", "schema.sql")


# ── 1. Schéma source (base fraîche) ──────────────────────────────────────────

def _schema_sql() -> str:
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return f.read()


def test_schema_declares_cascade_on_plugin_installations():
    """Base fraîche : la FK doit naître en CASCADE."""
    sql = _schema_sql()
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS plugin_installations\s*\((?P<body>.*?)\n\);",
        sql, re.DOTALL)
    assert m, "table plugin_installations introuvable dans schema.sql"
    fk_lines = [line for line in m.group("body").splitlines()
                if "REFERENCES plugins(id)" in line]
    assert fk_lines, "FK plugin_id → plugins(id) introuvable"
    assert all("ON DELETE CASCADE" in line for line in fk_lines), \
        f"FK sans CASCADE : {fk_lines!r}"


def test_apply_schema_carries_the_fk_migration_fixup():
    """Base existante : CREATE TABLE IF NOT EXISTS ne rejoue pas la contrainte —
    le fixup DROP/ADD de apply_schema est la SEULE voie de migration. Garde
    contre sa suppression accidentelle."""
    from app.services import db as db_mod
    src = inspect.getsource(db_mod.apply_schema)
    assert "plugin_installations_plugin_id_fkey" in src
    assert re.search(
        r"ADD\s+CONSTRAINT\s+plugin_installations_plugin_id_fkey\s+"
        r"FOREIGN KEY\s*\(plugin_id\)\s*REFERENCES plugins\(id\)\s*ON DELETE CASCADE",
        src), "le fixup doit recréer la FK en ON DELETE CASCADE"
    assert "confdeltype <> 'c'" in src, \
        "le fixup doit être conditionné (idempotent, pas de churn à chaque boot)"


# ── 2. Comportement de purge (ordre préservé) ────────────────────────────────

class _ScriptedCur:
    def __init__(self, removed_ids):
        self._removed_ids = removed_ids
        self.executed = []
        self.rowcount = len(removed_ids)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return [(i,) for i in self._removed_ids]


def test_purge_removed_deletes_campaigns_then_plugins():
    """campaigns (FK sans CASCADE, gérée en deux temps dans le code) doit être
    vidée AVANT plugins ; les installations, elles, partent par le CASCADE."""
    from app.admin.services import catalog as svc
    cur = _ScriptedCur([1])
    n = svc.purge_removed(cur)
    assert n == 1
    deletes = [sql for sql, _ in cur.executed if sql.startswith("DELETE")]
    assert len(deletes) == 2
    assert "campaigns" in deletes[0] and "plugins" in deletes[1], \
        f"ordre de purge inattendu : {deletes!r}"


# ── 3. Intégration : le scénario DGX complet sur vrai Postgres ───────────────

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
def test_legacy_db_migrated_then_purge_succeeds_and_is_replayable():
    psycopg2 = pytest.importorskip("psycopg2")
    url = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini")
    from app.services.db import apply_schema
    try:
        conn = psycopg2.connect(url)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres injoignable: {exc}")
    conn.autocommit = True

    def _deltype(cur):
        cur.execute("""
            SELECT confdeltype FROM pg_constraint
            WHERE conname = 'plugin_installations_plugin_id_fkey'
              AND conrelid = 'plugin_installations'::regclass
        """)
        row = cur.fetchone()
        return row[0] if row else None

    with conn.cursor() as cur:
        # Schéma en place, puis on simule la base LEGACY : FK sans CASCADE.
        apply_schema(url, _SCHEMA_PATH)
        cur.execute("ALTER TABLE plugin_installations DROP CONSTRAINT plugin_installations_plugin_id_fkey")
        cur.execute("""
            ALTER TABLE plugin_installations
            ADD CONSTRAINT plugin_installations_plugin_id_fkey
            FOREIGN KEY (plugin_id) REFERENCES plugins(id)
        """)
        assert _deltype(cur) == "a", "précondition : legacy = NO ACTION"

    # La migration (fixup d'apply_schema) doit passer la règle en CASCADE.
    apply_schema(url, _SCHEMA_PATH)
    with conn.cursor() as cur:
        assert _deltype(cur) == "c", "critère : confdeltype == 'c' (CASCADE)"

        # Scénario DGX : plugin 'removed' + installations → purge OK, installs supprimées.
        cur.execute("DELETE FROM plugins WHERE slug = 'it-fk-test'")
        cur.execute("""
            INSERT INTO plugins (slug, name, device_type, status)
            VALUES ('it-fk-test', 'IT FK test', 'it-fk-test', 'removed')
            RETURNING id
        """)
        pid = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO plugin_installations (plugin_id, client_uuid, installed_version)
            VALUES (%s, 'it-fk-uuid-1', '0.0.1'), (%s, 'it-fk-uuid-2', '0.0.1')
        """, (pid, pid))

        from app.admin.services import catalog as svc
        assert svc.purge_removed(cur) >= 1
        cur.execute("SELECT COUNT(*) FROM plugin_installations WHERE plugin_id = %s", (pid,))
        assert cur.fetchone()[0] == 0, "les installations doivent partir par CASCADE"

    # Rejouable sans erreur, règle stable.
    apply_schema(url, _SCHEMA_PATH)
    with conn.cursor() as cur:
        assert _deltype(cur) == "c"
    conn.close()
