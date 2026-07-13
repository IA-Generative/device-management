"""Quota LLM sur VRAI Postgres — cohérence multi-réplicas sous concurrence.

Critère 7 (volet intégration) : N stores (≙ N pods) incrémentent en parallèle,
le compteur final est EXACT (UPSERT atomique, aucune perte ni doublon).
Nécessite un Postgres accessible via DATABASE_URL (marqueur integration).
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.integration

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS llm_quota_counters (
    subject       TEXT        NOT NULL,
    window_start  TIMESTAMPTZ NOT NULL,
    count         INT         NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, window_start)
)
"""


@pytest.fixture()
def pg_ready():
    psycopg2 = pytest.importorskip("psycopg2")
    from app.services.db import db_url_bootstrap
    url = db_url_bootstrap()
    if not url:
        pytest.skip("DATABASE_URL non défini")
    try:
        conn = psycopg2.connect(url)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres injoignable: {exc}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(_TABLE_DDL)
        cur.execute("DELETE FROM llm_quota_counters WHERE subject LIKE 'it-test-%'")
    conn.close()
    return url


def test_concurrent_increments_across_stores_are_exact(pg_ready):
    from app.llm.throttle import PostgresQuotaStore

    subject = f"it-test-{os.getpid()}"
    replicas = [PostgresQuotaStore() for _ in range(2)]  # ≙ 2 pods llm-proxy
    total = 50

    def hit(i: int):
        return replicas[i % len(replicas)].incr(subject, limit=10_000, window_seconds=3600)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(hit, range(total)))

    counts = sorted(count for _, count, _ in results)
    # Exactitude : les 50 incréments donnent exactement 1..50 — pas de perte,
    # pas de doublon, quel que soit le réplica qui a servi la requête.
    assert counts == list(range(1, total + 1))


def test_limit_enforced_consistently_across_stores(pg_ready):
    from app.llm.throttle import PostgresQuotaStore

    subject = f"it-test-limit-{os.getpid()}"
    replica_a, replica_b = PostgresQuotaStore(), PostgresQuotaStore()
    outcomes = [
        store.incr(subject, limit=3, window_seconds=3600)[0]
        for store in (replica_a, replica_b, replica_a, replica_b, replica_a)
    ]
    assert outcomes == [True, True, True, False, False]
