"""Database connection pool, schema bootstrap, and helper utilities.

Extracted from app/main.py — these functions handle all direct PostgreSQL
interaction: connection pooling, schema application, and DB URL resolution.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("device-management")

try:
    import psycopg2
    import psycopg2.pool
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore


# ── Connection pool ───────────────────────────────────────

_pool: Any = None
_pool_lock = threading.Lock()
_POOL_MIN = 2
_POOL_MAX = 10


def _with_db(url: str, db_name: str) -> str:
    """Replace the database name in a PostgreSQL URL."""
    parsed = urlparse(url)
    path = f"/{db_name}"
    return urlunparse(parsed._replace(path=path))


def db_url() -> str | None:
    """Resolve the application database URL from environment."""
    from ..settings import settings
    return os.getenv("DATABASE_URL") or settings.database_url or None


def db_url_bootstrap() -> str | None:
    """Resolve the bootstrap database URL."""
    base = db_url()
    if not base:
        return None
    return _with_db(base, "bootstrap")


def get_pool():
    """Return (or lazily create) a ThreadedConnectionPool for the bootstrap DB."""
    global _pool
    if _pool is not None:
        return _pool
    if psycopg2 is None:
        return None
    url = db_url_bootstrap() or db_url()
    if not url:
        return None
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, url)
        except Exception as exc:
            logger.warning("Connection pool creation failed: %s", exc)
            return None
    return _pool


class PoolConn:
    """Context manager: borrows a connection from the pool, returns it on exit."""
    __slots__ = ("_conn", "_pool")

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()
        self._conn.autocommit = True

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        try:
            self._pool.putconn(self._conn)
        except Exception:
            pass
        self._conn = None


def pooled_conn():
    """Return a PoolConn context manager, or None if pool unavailable."""
    pool = get_pool()
    if pool is None:
        return None
    return PoolConn(pool)


def get_db_connection():
    """Get a standalone (non-pooled) database connection.

    Caller is responsible for closing it.
    """
    url = db_url_bootstrap() or db_url()
    if not url:
        return None
    if psycopg2 is None:
        return None
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


# ── DB URL helpers ────────────────────────────────────────

def admin_db_url(base_url: str) -> str | None:
    """Resolve admin database URL (with superuser credentials)."""
    explicit = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DM_DATABASE_ADMIN_URL")
    if explicit:
        return explicit
    parsed = urlparse(base_url)
    admin_user = (
        os.getenv("DB_ADMIN_USER")
        or os.getenv("POSTGRES_ADMIN_USER")
        or os.getenv("POSTGRES_USER")
        or "postgres"
    )
    admin_password = (
        os.getenv("DB_ADMIN_PASSWORD")
        or os.getenv("POSTGRES_ADMIN_PASSWORD")
        or os.getenv("POSTGRES_PASSWORD")
    )
    if admin_password:
        netloc = f"{admin_user}:{admin_password}@{parsed.hostname}"
    else:
        return None
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


# ── Schema bootstrap ─────────────────────────────────────

def ensure_database_exists(db_url_str: str, db_name: str = "bootstrap") -> None:
    """Create the database if it doesn't exist."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed.")
    admin_url = _with_db(db_url_str, "postgres")
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()


def ensure_dev_role(admin_url: str) -> None:
    """Create the 'dev' role if it doesn't exist."""
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'dev'")
            if not cur.fetchone():
                cur.execute("CREATE ROLE dev LOGIN PASSWORD 'dev'")
            try:
                cur.execute("ALTER ROLE dev NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
            except psycopg2.Error:
                logger.warning("Skipping ALTER ROLE dev (insufficient privilege)")
    finally:
        conn.close()


def ensure_dev_privileges(admin_bootstrap_url: str) -> None:
    """Grant dev role the minimum required privileges."""
    conn = psycopg2.connect(admin_bootstrap_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("GRANT CONNECT ON DATABASE bootstrap TO dev")
            cur.execute("GRANT USAGE ON SCHEMA public TO dev")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dev")
            cur.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dev"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO dev"
            )
    finally:
        conn.close()


# Verrou consultatif : sérialise l'application du schéma quand N pods démarrent
# en même temps (sinon course Postgres « tuple concurrently updated » sur les
# GRANT concurrents → le batch entier est annulé sur TOUS les pods et les tables
# config_state/… manquent, laissant le readiness gate à 503).
_SCHEMA_APPLY_LOCK_ID = 727270910


def apply_schema(db_url_str: str, schema_path: str) -> None:
    """Apply schema.sql with pre-migration fixups."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed.")
    if not os.path.isfile(schema_path):
        raise FileNotFoundError(f"Schema SQL not found: {schema_path}")
    with open(schema_path, encoding="utf-8") as f:
        sql = f.read()
    conn = psycopg2.connect(db_url_str)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_APPLY_LOCK_ID,))
            cur.execute("""
                DO $$ BEGIN
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'campaigns') THEN
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'campaigns' AND column_name = 'environment'
                    ) THEN
                      ALTER TABLE campaigns ADD COLUMN environment VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'campaigns' AND column_name = 'plugin_id'
                    ) THEN
                      ALTER TABLE campaigns ADD COLUMN plugin_id INT REFERENCES plugins(id);
                    END IF;
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'campaigns' AND column_name = 'version_id'
                    ) THEN
                      ALTER TABLE campaigns ADD COLUMN version_id INT REFERENCES plugin_versions(id);
                    END IF;
                  END IF;
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'plugins') THEN
                    IF EXISTS (
                      SELECT 1 FROM pg_constraint
                      WHERE conname = 'plugins_slug_key' AND conrelid = 'plugins'::regclass
                    ) THEN
                      ALTER TABLE plugins DROP CONSTRAINT plugins_slug_key;
                    END IF;
                    -- DM-4 : metadata d'auto-update (CREATE TABLE IF NOT EXISTS n'ajoute
                    -- pas de colonne à une table plugins existante → ALTER explicite ici).
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'plugins' AND column_name = 'extension_id'
                    ) THEN
                      ALTER TABLE plugins ADD COLUMN extension_id VARCHAR(64);
                    END IF;
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'plugins' AND column_name = 'gecko_id'
                    ) THEN
                      ALTER TABLE plugins ADD COLUMN gecko_id VARCHAR(128);
                    END IF;
                  END IF;
                  -- Flags v2 : catalogue scopé par plugin + marquage orphelins
                  -- (CREATE TABLE IF NOT EXISTS n'ajoute pas de colonne à une
                  -- table existante → ALTER explicite ; l'unicité globale sur
                  -- name laisse place à (plugin_slug, name), cf. schema.sql).
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'feature_flags') THEN
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'feature_flags' AND column_name = 'plugin_slug'
                    ) THEN
                      ALTER TABLE feature_flags ADD COLUMN plugin_slug VARCHAR(100) NOT NULL DEFAULT '';
                    END IF;
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'feature_flags' AND column_name = 'deprecated'
                    ) THEN
                      ALTER TABLE feature_flags ADD COLUMN deprecated BOOLEAN NOT NULL DEFAULT false;
                    END IF;
                    -- Version min PAR FLAG (création manuelle admin ; NULL = toutes)
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'feature_flags' AND column_name = 'min_plugin_version'
                    ) THEN
                      ALTER TABLE feature_flags ADD COLUMN min_plugin_version VARCHAR(50);
                    END IF;
                    IF EXISTS (
                      SELECT 1 FROM pg_constraint
                      WHERE conname = 'feature_flags_name_key' AND conrelid = 'feature_flags'::regclass
                    ) THEN
                      ALTER TABLE feature_flags DROP CONSTRAINT feature_flags_name_key;
                    END IF;
                  END IF;
                  -- feature_flag_overrides.updated_at : référencée depuis toujours
                  -- par get_flag_overrides + l'ON CONFLICT de create_override, mais
                  -- jamais créée → /admin/flags/{id} cassait (UndefinedColumn).
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'feature_flag_overrides') THEN
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'feature_flag_overrides' AND column_name = 'updated_at'
                    ) THEN
                      ALTER TABLE feature_flag_overrides ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now();
                    END IF;
                  END IF;
                  -- feature_flags.default_value TRI-ÉTAT : le défaut colonne passe
                  -- de `true` à NULL (transparent). Les lignes EXISTANTES gardent
                  -- leur valeur (true=forcé ON / false=forcé OFF) ; seuls les
                  -- NOUVEAUX flags naissent transparents (le reconcile insère NULL).
                  -- ALTER ... DROP DEFAULT est un no-op si déjà absent (idempotent).
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'feature_flags') THEN
                    ALTER TABLE feature_flags ALTER COLUMN default_value DROP DEFAULT;
                  END IF;
                  -- plugin_installations.plugin_id → ON DELETE CASCADE (aligne
                  -- sur les FK sœurs). La contrainte d'origine (NO ACTION)
                  -- bloquait la purge d'un plugin 'removed' ayant des
                  -- installations (constaté DGX). CREATE TABLE IF NOT EXISTS ne
                  -- rejoue pas la contrainte sur une base existante → DROP/ADD
                  -- explicite, UNIQUEMENT si la règle n'est pas déjà CASCADE
                  -- (idempotent, rejouable).
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'plugin_installations') THEN
                    IF EXISTS (
                      SELECT 1 FROM pg_constraint
                      WHERE conname = 'plugin_installations_plugin_id_fkey'
                        AND conrelid = 'plugin_installations'::regclass
                        AND confdeltype <> 'c'
                    ) THEN
                      ALTER TABLE plugin_installations DROP CONSTRAINT plugin_installations_plugin_id_fkey;
                      ALTER TABLE plugin_installations
                        ADD CONSTRAINT plugin_installations_plugin_id_fkey
                        FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE;
                    END IF;
                  END IF;
                  -- Journal d'audit : plugin_slug PERSISTÉ (rempli à l'écriture
                  -- par le helper audit_log). Backfill ONE-SHOT de l'historique à
                  -- la création de la colonne — même dérivation que la lecture
                  -- (ressource plugin:*, payload plugin_slug/plugin_id, flag:N) ;
                  -- fige la valeur avant toute suppression future de plugin/flag.
                  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'admin_audit_log') THEN
                    IF NOT EXISTS (
                      SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'admin_audit_log' AND column_name = 'plugin_slug'
                    ) THEN
                      ALTER TABLE admin_audit_log ADD COLUMN plugin_slug VARCHAR(100);
                      UPDATE admin_audit_log a SET plugin_slug = COALESCE(
                        CASE WHEN a.resource_type = 'plugin' AND a.resource_id !~ '^[0-9]+$'
                              AND a.resource_id <> '*' THEN a.resource_id END,
                        CASE WHEN a.resource_type = 'plugin' AND a.resource_id ~ '^[0-9]+$' THEN
                          (SELECT p.slug FROM plugins p WHERE p.id = a.resource_id::int) END,
                        a.payload->>'plugin_slug',
                        (SELECT p.slug FROM plugins p
                          WHERE (a.payload->>'plugin_id') ~ '^[0-9]+$'
                            AND p.id = (a.payload->>'plugin_id')::int),
                        CASE WHEN a.resource_type = 'flag' AND a.resource_id ~ '^[0-9]+$' THEN
                          (SELECT NULLIF(ff.plugin_slug, '') FROM feature_flags ff
                            WHERE ff.id = a.resource_id::int) END
                      );
                    END IF;
                  END IF;
                END $$;
            """)
            cur.execute(sql)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_plugins_slug_active
                ON plugins(slug) WHERE status <> 'removed'
            """)
    finally:
        conn.close()


def wait_for_db(db_url_str: str, timeout_seconds: int = 30, interval_seconds: float = 1.0) -> None:
    """Block until the database is reachable or timeout."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed.")
    deadline = time.time() + timeout_seconds
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(db_url_str)
            conn.close()
            return
        except psycopg2.OperationalError as exc:
            last_exc = exc
            time.sleep(interval_seconds)
    if last_exc:
        raise last_exc
