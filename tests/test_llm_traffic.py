"""Trafic LLM bucketé (app/llm/traffic.py + service admin + table llm_traffic).

Le proxy n'écrit JAMAIS une ligne par appel (rafales d'embeddings pendant
l'indexation RAG) : accumulation mémoire par bucket 5 min, flush UPSERT
périodique — pattern llm_quota_counters. Ces tests verrouillent :
l'agrégation, la classification, le garde-fou mémoire, la re-fusion sur échec
de flush, la lecture dashboard, et (intégration) le cycle complet sur PG réel.
"""
import datetime
import os
from unittest import mock

import pytest

from app.admin.services import llm_traffic as svc
from app.llm import traffic


@pytest.fixture(autouse=True)
def _reset_traffic_state():
    """État module propre par test ; pas de thread de flush en tests."""
    with traffic._lock:
        traffic._acc.clear()
    traffic._flusher_started = True   # neutralise _ensure_flusher
    yield
    with traffic._lock:
        traffic._acc.clear()


# ── record : agrégation en mémoire ───────────────────────────────────────────

def test_record_aggregates_same_bucket():
    with mock.patch("time.time", return_value=1_800_000_000):
        traffic.record(route="chat/completions", model="octen-4b", status=200,
                       duration_seconds=0.5, usage={"total_tokens": 100})
        traffic.record(route="chat/completions", model="octen-4b", status=200,
                       duration_seconds=1.5, usage={"total_tokens": 50})
    bucket = 1_800_000_000 // traffic.BUCKET_SECONDS * traffic.BUCKET_SECONDS
    key = (bucket, "chat", "octen-4b", "2xx")
    assert traffic._acc[key] == [2, 2000, 1500, 150]   # count, sum, max, tokens


def test_record_routes_and_status_classes():
    with mock.patch("time.time", return_value=1_800_000_000):
        traffic.record(route="embeddings", model="embedding", status=401, duration_seconds=0.04)
        traffic.record(route="models", model=None, status=502, duration_seconds=0.01)
    keys = set(traffic._acc)
    assert any(k[1] == "embeddings" and k[3] == "4xx" for k in keys)
    assert any(k[1] == "models" and k[2] == "" and k[3] == "5xx" for k in keys)


def test_record_never_grows_unbounded():
    """Base longtemps absente : on jette les buckets les plus anciens, on ne
    croît pas sans borne (et on ne lève JAMAIS depuis record)."""
    with traffic._lock:
        for i in range(traffic.MAX_PENDING_KEYS):
            traffic._acc[(i, "chat", "m", "2xx")] = [1, 1, 1, 0]
    with mock.patch("time.time", return_value=1_800_000_000):
        traffic.record(route="chat", model="m", status=200, duration_seconds=0.1)
    assert len(traffic._acc) == traffic.MAX_PENDING_KEYS
    assert (0, "chat", "m", "2xx") not in traffic._acc   # le plus ancien a sauté


# ── flush : UPSERT groupé + re-fusion sur échec ──────────────────────────────

class _Cur:
    def __init__(self, fail=False):
        self.fail = fail
        self.executed = []

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, fail=False):
        self._cur = _Cur(fail)

    def cursor(self):
        return self._cur


def test_flush_upserts_and_drains():
    with mock.patch("time.time", return_value=1_800_000_000):
        traffic.record(route="chat", model="m", status=200, duration_seconds=0.2)
        traffic.record(route="embeddings", model="e", status=200, duration_seconds=0.05)
    conn = _Conn()
    assert traffic.flush_now(conn=conn) == 2
    assert not traffic._acc, "accumulateur vidé après flush"
    upserts = [sql for sql, _ in conn._cur.executed if "INSERT INTO llm_traffic" in sql]
    assert len(upserts) == 2
    assert all("ON CONFLICT (bucket_ts, route, model, status_class)" in sql for sql in upserts)
    assert all("GREATEST(llm_traffic.duration_ms_max" in sql for sql in upserts)


def test_flush_failure_remerges_counts():
    """Blip de la base : les compteurs repartent dans l'accumulateur — un pic
    de trafic ne disparaît pas sur un échec de flush."""
    with mock.patch("time.time", return_value=1_800_000_000):
        traffic.record(route="chat", model="m", status=200, duration_seconds=0.2)
    assert traffic.flush_now(conn=_Conn(fail=True)) == 0
    bucket = 1_800_000_000 // traffic.BUCKET_SECONDS * traffic.BUCKET_SECONDS
    assert traffic._acc[(bucket, "chat", "m", "2xx")][0] == 1


def test_flush_empty_is_noop():
    assert traffic.flush_now(conn=_Conn()) == 0


# ── service admin : série + tuiles ───────────────────────────────────────────

class _ScriptedCur:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


def test_series_shapes_rows_and_whitelists_period():
    t0 = datetime.datetime(2026, 7, 14, 15, 0, tzinfo=datetime.UTC)
    cur = _ScriptedCur([(t0, 12, 340, 3)])
    out = svc.series(cur, "1J")
    assert out == [{"date": t0.isoformat(), "chat": 12, "embeddings": 340, "errors": 3}]
    sql, params = cur.executed[0]
    assert params == ("hour", "24 hours")

    cur2 = _ScriptedCur([])
    svc.series(cur2, "nimporte-quoi")   # période inconnue → repli 1J, pas d'injection
    assert cur2.executed[0][1] == ("hour", "24 hours")


def test_tiles_ratios_and_zero_division():
    cur = _ScriptedCur([(400, 80_000, 20, 300, 12_345)])
    t = svc.tiles(cur)
    assert t == {"calls_24h": 400, "avg_ms": 200, "error_rate": 5.0,
                 "embed_share": 75, "tokens_24h": 12_345}
    assert svc.tiles(_ScriptedCur([(0, 0, 0, 0, 0)]))["error_rate"] == 0


# ── intégration : cycle complet sur vrai Postgres ────────────────────────────

@pytest.mark.integration
def test_full_cycle_on_real_postgres():
    psycopg2 = pytest.importorskip("psycopg2")
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini")
    from app.services.db import apply_schema
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        conn = psycopg2.connect(url)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres injoignable: {exc}")
    conn.autocommit = True
    apply_schema(url, os.path.join(repo_root, "db", "schema.sql"))

    with mock.patch("time.time", return_value=1_800_000_000):
        traffic.record(route="chat/completions", model="octen-4b", status=200,
                       duration_seconds=0.3, usage={"total_tokens": 42})
        traffic.record(route="embeddings", model="embedding", status=401,
                       duration_seconds=0.05)
    assert traffic.flush_now(conn=conn) == 2
    # Re-flush du même bucket : l'UPSERT additionne, pas de doublon de clef.
    with mock.patch("time.time", return_value=1_800_000_001):
        traffic.record(route="chat/completions", model="octen-4b", status=200,
                       duration_seconds=0.7, usage={"total_tokens": 8})
    assert traffic.flush_now(conn=conn) == 1

    with conn.cursor() as cur:
        cur.execute("""
            SELECT count, duration_ms_sum, tokens_sum FROM llm_traffic
            WHERE route = 'chat' AND model = 'octen-4b' AND status_class = '2xx'
              AND bucket_ts = to_timestamp(%s)
        """, (1_800_000_000 // traffic.BUCKET_SECONDS * traffic.BUCKET_SECONDS,))
        assert cur.fetchone() == (2, 1000, 50)
        cur.execute("DELETE FROM llm_traffic WHERE model IN ('octen-4b', 'embedding')")
    conn.close()
