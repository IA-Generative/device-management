"""Intégration (vrai Postgres) — version générale par plugin (issue #40).

Pourquoi ce module : la règle « quelle version les canaux natifs annoncent à
tout le parc » est du SQL (statuts, servabilité, général vs dernière publiée),
et la migration passe par un bloc de db/schema.sql que le déploiement applique
par psql sur des bases EXISTANTES. Les tests unitaires mockent psycopg2 ; ici
Postgres est juge.

Marqué `integration` (exclu du gate unitaire). Tout se déroule dans une
transaction annulée en sortie, y compris le DROP COLUMN qui simule une base
d'avant 0.9.20 : aucune donnée ni structure ne survit au test.

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_general_version_pg.py -m integration`
"""
from __future__ import annotations

import os
import re
import sys

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "db", "schema.sql")

_SLUG = "it-general-version"
_DEVICE_TYPE = "it-general-version"


def _load_main():
    """Charge app.main avec le VRAI psycopg2 (d'autres modules en injectent un faux)."""
    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    os.environ.setdefault("DM_CONFIG_ENABLED", "true")
    os.environ.setdefault("DM_AUTH_VERIFY_ACCESS_TOKEN", "false")
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    sys.modules.pop("psycopg2", None)
    import psycopg2  # le vrai module
    for name in ("app.main", "app.settings"):
        sys.modules.pop(name, None)
    import app.main as mod
    mod.psycopg2 = psycopg2
    return mod


def _migration_block() -> str:
    """Le bloc de migration tel que le déploiement l'exécute (extrait de schema.sql)."""
    text = open(_SCHEMA_PATH, encoding="utf-8").read()
    match = re.search(r"-- Version générale \(issue #40\).*?\nDO \$\$ BEGIN.*?\nEND \$\$;", text, re.S)
    assert match, "bloc de migration introuvable dans db/schema.sql"
    return match.group(0)


@pytest.fixture(scope="module")
def _db_url() -> str:
    pytest.importorskip("psycopg2")
    url = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini")
    import psycopg2
    try:
        psycopg2.connect(url, connect_timeout=3).close()
    except Exception as exc:  # pragma: no cover — dépend de l'infra locale
        pytest.skip(f"Postgres injoignable: {exc}")
    from app.services.db import apply_schema
    apply_schema(url, _SCHEMA_PATH)
    return url


@pytest.fixture(scope="module")
def mod(_db_url):
    return _load_main()


@pytest.fixture
def fixt(_db_url):
    """Transaction annulée en sortie + un plugin. Renvoie (cur, plugin_id, add)."""
    import psycopg2
    conn = psycopg2.connect(_db_url)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO plugins (slug, name, device_type, status)
        VALUES (%s, 'IT version générale', %s, 'active') RETURNING id
    """, (_SLUG, _DEVICE_TYPE))
    plugin_id = cur.fetchone()[0]

    def add(version: str, *, status: str = "published", age_days: int = 0,
            s3_path: str | None = "set", mode: str = "managed") -> int:
        path = f"{_DEVICE_TYPE}/{version}.oxt" if s3_path == "set" else s3_path
        cur.execute("""
            INSERT INTO artifacts (device_type, version, s3_path, checksum, is_active)
            VALUES (%s, %s, %s, %s, true) RETURNING id
        """, (_DEVICE_TYPE, version, path, "sha256:" + "0" * 64))
        artifact_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO plugin_versions (plugin_id, artifact_id, version, status,
                                         distribution_mode, published_at)
            VALUES (%s, %s, %s, %s, %s, now() - make_interval(days => %s)) RETURNING id
        """, (plugin_id, artifact_id, version, status, mode, age_days))
        return cur.fetchone()[0]

    try:
        yield cur, plugin_id, add
    finally:
        conn.rollback()
        cur.close()
        conn.close()


def _general_id(cur, plugin_id):
    cur.execute("SELECT general_version_id FROM plugins WHERE id = %s", (plugin_id,))
    return cur.fetchone()[0]


def _id_of(cur, plugin_id, version):
    cur.execute("SELECT id FROM plugin_versions WHERE plugin_id = %s AND version = %s",
                (plugin_id, version))
    return cur.fetchone()[0]


# ── Migration : base d'avant 0.9.20 ──────────────────────────────────────

def test_migration_adds_the_column_and_backfills_the_latest_servable(fixt):
    cur, plugin_id, add = fixt
    cur.execute("ALTER TABLE plugins DROP COLUMN general_version_id")   # base 0.9.19
    add("1.0.0", status="deprecated", age_days=9)
    add("1.1.0", age_days=5)
    add("1.2.0", age_days=1, s3_path=None)          # publiée avant la fin de l'upload
    add("1.3.0-rc1", status="experimental", age_days=0)
    cur.execute(_migration_block())
    assert _general_id(cur, plugin_id) == _id_of(cur, plugin_id, "1.1.0")


def test_migration_is_idempotent_and_never_rewrites_an_admin_choice(fixt):
    cur, plugin_id, add = fixt
    cur.execute("ALTER TABLE plugins DROP COLUMN general_version_id")
    add("1.0.0", age_days=3)
    cur.execute(_migration_block())
    first = _general_id(cur, plugin_id)
    cur.execute(_migration_block())                 # redéploiement
    assert _general_id(cur, plugin_id) == first
    cur.execute("UPDATE plugins SET general_version_id = NULL WHERE id = %s", (plugin_id,))
    cur.execute(_migration_block())                 # choix « pas de générale » respecté
    assert _general_id(cur, plugin_id) is None


def test_migration_on_a_plugin_without_versions_leaves_null(fixt):
    cur, plugin_id, _add = fixt
    cur.execute("ALTER TABLE plugins DROP COLUMN general_version_id")
    cur.execute(_migration_block())
    assert _general_id(cur, plugin_id) is None


def test_deleting_the_general_version_clears_the_pointer(fixt):
    cur, plugin_id, add = fixt
    vid = add("1.0.0")
    cur.execute("UPDATE plugins SET general_version_id = %s WHERE id = %s", (vid, plugin_id))
    cur.execute("DELETE FROM plugin_versions WHERE id = %s", (vid,))
    assert _general_id(cur, plugin_id) is None      # ON DELETE SET NULL


def test_full_schema_can_be_reapplied(_db_url):
    """Le déploiement rejoue tout schema.sql à chaque bascule."""
    from app.services.db import apply_schema
    apply_schema(_db_url, _SCHEMA_PATH)


# ── Résolveur ────────────────────────────────────────────────────────────

def test_without_general_the_latest_published_servable_is_announced(fixt, mod):
    cur, plugin_id, add = fixt
    add("1.0.0", age_days=3)
    add("1.1.0", age_days=1)
    assert mod._general_version(cur, plugin_id) == "1.1.0"


def test_publishing_does_not_move_the_general_version(fixt, mod):
    cur, plugin_id, add = fixt
    v1 = add("1.0.0", age_days=3)
    cur.execute("UPDATE plugins SET general_version_id = %s WHERE id = %s", (v1, plugin_id))
    add("1.1.0", age_days=0)                         # canary : publiée, pas générale
    assert mod._general_version(cur, plugin_id) == "1.0.0"


def test_a_deprecated_general_version_is_still_announced(fixt, mod):
    cur, plugin_id, add = fixt
    v1 = add("1.0.0", status="deprecated", age_days=3)
    add("1.1.0")
    cur.execute("UPDATE plugins SET general_version_id = %s WHERE id = %s", (v1, plugin_id))
    assert mod._general_version(cur, plugin_id) == "1.0.0"


@pytest.mark.parametrize("status,s3_path", [("yanked", "set"), ("draft", "set"),
                                            ("published", None), ("published", "")])
def test_an_unservable_general_version_silences_the_channels(fixt, mod, status, s3_path):
    cur, plugin_id, add = fixt
    add("0.9.0", age_days=9)                         # une publiée servable existe…
    vid = add("1.0.0", status=status, s3_path=s3_path)
    cur.execute("UPDATE plugins SET general_version_id = %s WHERE id = %s", (vid, plugin_id))
    assert mod._general_version(cur, plugin_id) is None   # …mais aucun repli


@pytest.mark.parametrize("status,expected", [("published", "1.1.0"), ("deprecated", "1.1.0"),
                                             ("experimental", "1.1.0"), ("draft", None),
                                             ("yanked", None)])
def test_requested_version_follows_the_requestable_statuses(fixt, mod, status, expected):
    cur, plugin_id, add = fixt
    add("1.1.0", status=status)
    assert mod._servable_version(cur, plugin_id, "1.1.0", mod._REQUESTABLE_STATUSES) == expected


def test_requested_version_of_another_plugin_is_not_served(fixt, mod):
    cur, plugin_id, add = fixt
    add("1.1.0")
    assert mod._servable_version(cur, plugin_id + 100000, "1.1.0", mod._REQUESTABLE_STATUSES) is None


def test_variant_release_follows_the_general_version(fixt, mod):
    cur, plugin_id, add = fixt
    v1 = add("1.0.0", age_days=3)
    v2 = add("1.1.0", age_days=0)
    for vid, version in ((v1, "1.0.0"), (v2, "1.1.0")):
        cur.execute("""
            INSERT INTO artifacts (device_type, platform_variant, version, s3_path, checksum)
            VALUES (%s, 'gecko-dgx', %s, %s, %s) RETURNING id
        """, (_DEVICE_TYPE, version, f"gecko/{_SLUG}-{version}.xpi", "sha256:" + "1" * 64))
        aid = cur.fetchone()[0]
        cur.execute("""INSERT INTO plugin_version_artifacts (plugin_version_id, artifact_id, platform_variant)
                       VALUES (%s, %s, 'gecko-dgx')""", (vid, aid))
    assert mod._latest_variant_release(cur, plugin_id, "gecko-dgx")["version"] == "1.1.0"
    cur.execute("UPDATE plugins SET general_version_id = %s WHERE id = %s", (v1, plugin_id))
    rel = mod._latest_variant_release(cur, plugin_id, "gecko-dgx")
    assert rel["version"] == "1.0.0"
    assert rel["checksum"] == "sha256:" + "1" * 64
    assert mod._latest_variant_release(cur, plugin_id, "gecko-other") is None


# ── Admin (service, vrai SQL) ────────────────────────────────────────────

def test_admin_can_set_and_clear_the_general_version(fixt):
    from app.admin.services import catalog as svc
    cur, plugin_id, add = fixt
    v1 = add("1.0.0", age_days=3)
    v2 = add("1.1.0", status="deprecated")
    assert svc.set_general_version(cur, plugin_id, v1) == (None, "1.0.0")
    assert svc.set_general_version(cur, plugin_id, v2) == ("1.0.0", "1.1.0")
    assert svc.get_general_version(cur, plugin_id)["version"] == "1.1.0"
    assert svc.set_general_version(cur, plugin_id, None) == ("1.1.0", None)


@pytest.mark.parametrize("status,s3_path", [("experimental", "set"), ("yanked", "set"),
                                            ("draft", "set"), ("published", None)])
def test_admin_cannot_make_general_what_cannot_be_served(fixt, status, s3_path):
    from app.admin.services import catalog as svc
    cur, plugin_id, add = fixt
    vid = add("1.0.0", status=status, s3_path=s3_path)
    with pytest.raises(svc.GeneralVersionError):
        svc.set_general_version(cur, plugin_id, vid)
    assert _general_id(cur, plugin_id) is None


def test_admin_cannot_use_a_version_of_another_plugin(fixt):
    from app.admin.services import catalog as svc
    cur, plugin_id, add = fixt
    vid = add("1.0.0")
    with pytest.raises(svc.GeneralVersionError):
        svc.set_general_version(cur, plugin_id + 100000, vid)


def test_the_general_version_cannot_be_withdrawn_but_can_be_deprecated(fixt):
    from app.admin.services import catalog as svc
    cur, plugin_id, add = fixt
    vid = add("1.0.0")
    svc.set_general_version(cur, plugin_id, vid)
    with pytest.raises(svc.GeneralVersionError):
        svc.update_version_status(cur, vid, "yanked", plugin_id=plugin_id)
    assert svc.update_version_status(cur, vid, "deprecated", plugin_id=plugin_id) is True
    assert svc.update_version_status(cur, vid, "published", plugin_id=plugin_id + 100000) is False
