"""Intégration (vrai Postgres) — version annoncée par le feed LibreOffice update.xml.

Pourquoi ce module : la règle « dernière version publiée ET servable » de
`_latest_servable_version` est du SQL (jointure artifacts, modes de
distribution, ORDER BY published_at). Les tests de
test_libreoffice_update_feed.py mockent psycopg2 et réappliquent la règle en
Python : un filtre affaibli ou un tri supprimé y restaient verts. Ici,
Postgres est juge.

Marqué `integration` (exclu du gate unitaire). Tout se déroule dans une
transaction annulée en sortie : aucune donnée ne survit au test.

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_libreoffice_update_feed_pg.py -m integration`
"""
from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "db", "schema.sql")

_SLUG = "it-lo-feed"
_DEVICE_TYPE = "it-lo-feed"


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
def latest(_db_url):
    return _load_main()._latest_servable_version


@pytest.fixture
def fixt(_db_url):
    """Transaction annulée en sortie + un plugin. Renvoie (cur, add_version)."""
    import psycopg2
    conn = psycopg2.connect(_db_url)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO plugins (slug, name, device_type, status)
        VALUES (%s, 'IT feed LibreOffice', %s, 'active') RETURNING id
    """, (_SLUG, _DEVICE_TYPE))
    plugin_id = cur.fetchone()[0]

    def add_version(version: str, *, mode: str = "managed", s3_path: str | None = "set",
                    with_artifact: bool = True, download_url: str | None = None,
                    status: str = "published", age_days: int | None = 0) -> None:
        """Une version ; `age_days` fixe published_at (None → NULL)."""
        artifact_id = None
        if with_artifact:
            path = f"{_DEVICE_TYPE}/{version}.oxt" if s3_path == "set" else s3_path
            cur.execute("""
                INSERT INTO artifacts (device_type, version, s3_path, is_active)
                VALUES (%s, %s, %s, true) RETURNING id
            """, (_DEVICE_TYPE, version, path))
            artifact_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO plugin_versions (plugin_id, artifact_id, version, download_url,
                                         status, distribution_mode, published_at)
            VALUES (%s, %s, %s, %s, %s, %s,
                    CASE WHEN %s::int IS NULL THEN NULL
                         ELSE now() - make_interval(days => %s::int) END)
        """, (plugin_id, artifact_id, version, download_url, status, mode, age_days, age_days))

    try:
        yield cur, plugin_id, add_version
    finally:
        conn.rollback()
        cur.close()
        conn.close()


def test_announces_the_most_recently_published_version(fixt, latest):
    cur, plugin_id, add = fixt
    add("1.0.0", age_days=3)
    add("1.2.0", age_days=1)
    add("1.1.0", age_days=2)
    assert latest(cur, plugin_id) == "1.2.0"


def test_published_at_null_ranks_last(fixt, latest):
    cur, plugin_id, add = fixt
    add("2.0.0", age_days=None)
    add("1.0.0", age_days=5)
    assert latest(cur, plugin_id) == "1.0.0"


@pytest.mark.parametrize("unservable", [
    {"with_artifact": False},                          # managed sans artefact
    {"s3_path": None},                                 # artefact créé, upload pas fini
    {"s3_path": ""},
    {"mode": "download_link", "with_artifact": False},  # lien externe sans URL
    {"mode": "store", "with_artifact": False, "download_url": ""},
    {"mode": "manual"},                                # jamais servi par le DM
    {"status": "experimental"},                        # pull opt-in, jamais annoncé
    {"status": "draft"},
])
def test_skips_a_newer_unservable_version(fixt, latest, unservable):
    """La plus récente n'est pas servable : on annonce la précédente, pas elle."""
    cur, plugin_id, add = fixt
    add("1.0.0", age_days=2)
    add("1.1.0", age_days=1, **unservable)
    assert latest(cur, plugin_id) == "1.0.0"


@pytest.mark.parametrize("mode", ["download_link", "store"])
def test_external_link_modes_are_servable_with_an_url(fixt, latest, mode):
    cur, plugin_id, add = fixt
    add("1.0.0", age_days=2)
    add("1.1.0", age_days=1, mode=mode, with_artifact=False,
        download_url="https://example.org/mirai.oxt")
    assert latest(cur, plugin_id) == "1.1.0"


def test_none_when_nothing_is_servable(fixt, latest):
    cur, plugin_id, add = fixt
    add("1.0.0", s3_path=None)
    assert latest(cur, plugin_id) is None
