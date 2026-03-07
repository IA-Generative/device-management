from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:
    import psycopg2  # type: ignore
    from psycopg2.extras import Json  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    psycopg2 = None  # type: ignore
    Json = None  # type: ignore


@dataclass
class QueueJob:
    id: str
    topic: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    created_at: datetime | None = None
    next_attempt_at: datetime | None = None


class PostgresQueue:
    def __init__(
        self,
        dsn: str,
        *,
        lock_ttl_seconds: int = 60,
        default_max_attempts: int = 8,
        retry_base_seconds: int = 2,
        retry_max_seconds: int = 300,
        retry_jitter_seconds: float = 1.0,
    ) -> None:
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required for Postgres queue support")
        self._dsn = dsn
        self._lock_ttl_seconds = max(1, int(lock_ttl_seconds))
        self._default_max_attempts = max(1, int(default_max_attempts))
        self._retry_base_seconds = max(1, int(retry_base_seconds))
        self._retry_max_seconds = max(self._retry_base_seconds, int(retry_max_seconds))
        self._retry_jitter_seconds = max(0.0, float(retry_jitter_seconds))

    def _connect(self):
        return psycopg2.connect(self._dsn)

    @staticmethod
    def _payload_to_dict(raw_payload: Any) -> dict[str, Any]:
        if isinstance(raw_payload, dict):
            return raw_payload
        if isinstance(raw_payload, str):
            try:
                data = json.loads(raw_payload)
            except Exception:
                return {"raw": raw_payload}
            return data if isinstance(data, dict) else {"raw": raw_payload}
        return {"raw": str(raw_payload)}

    def enqueue(
        self,
        *,
        topic: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        run_after_seconds: int = 0,
        max_attempts: int | None = None,
    ) -> tuple[str, str]:
        if not topic or not isinstance(payload, dict):
            raise ValueError("queue enqueue requires topic and object payload")
        attempts_limit = max(1, int(max_attempts or self._default_max_attempts))
        delay = max(0, int(run_after_seconds or 0))
        with self._connect() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO queue_jobs (
                        topic, payload, dedupe_key, max_attempts, next_attempt_at
                    ) VALUES (
                        %s, %s, %s, %s, now() + (%s || ' seconds')::interval
                    )
                    ON CONFLICT (topic, dedupe_key)
                    DO UPDATE SET updated_at = now()
                    RETURNING id::text, status
                    """,
                    (
                        topic,
                        Json(payload),
                        dedupe_key,
                        attempts_limit,
                        delay,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("queue enqueue failed without returned row")
                return str(row[0]), str(row[1])

    def claim_jobs(self, *, worker_id: str, limit: int = 50) -> list[QueueJob]:
        batch_size = max(1, int(limit))
        with self._connect() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE queue_jobs
                    SET
                        status = 'processing',
                        lock_owner = %s,
                        locked_at = now(),
                        attempts = attempts + 1,
                        updated_at = now()
                    WHERE id IN (
                        SELECT id
                        FROM queue_jobs
                        WHERE
                            status = 'pending'
                            AND next_attempt_at <= now()
                            AND (
                                locked_at IS NULL
                                OR locked_at < now() - (%s || ' seconds')::interval
                            )
                        ORDER BY next_attempt_at ASC, created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    RETURNING
                        id::text,
                        topic,
                        payload,
                        attempts,
                        max_attempts,
                        dedupe_key,
                        created_at,
                        next_attempt_at
                    """,
                    (
                        worker_id,
                        self._lock_ttl_seconds,
                        batch_size,
                    ),
                )
                rows = cur.fetchall()
                conn.commit()

        jobs: list[QueueJob] = []
        for row in rows:
            jobs.append(
                QueueJob(
                    id=str(row[0]),
                    topic=str(row[1]),
                    payload=self._payload_to_dict(row[2]),
                    attempts=int(row[3] or 0),
                    max_attempts=int(row[4] or self._default_max_attempts),
                    dedupe_key=str(row[5]) if row[5] else None,
                    created_at=row[6],
                    next_attempt_at=row[7],
                )
            )
        return jobs

    def ack(self, *, job_id: str, worker_id: str) -> bool:
        with self._connect() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE queue_jobs
                    SET
                        status = 'done',
                        lock_owner = NULL,
                        locked_at = NULL,
                        completed_at = now(),
                        updated_at = now()
                    WHERE id = %s::uuid
                      AND status = 'processing'
                      AND lock_owner = %s
                    """,
                    (job_id, worker_id),
                )
                return cur.rowcount > 0

    def _retry_delay_seconds(self, attempts: int) -> int:
        exponent = max(0, int(attempts) - 1)
        delay = min(self._retry_max_seconds, self._retry_base_seconds * (2 ** exponent))
        if self._retry_jitter_seconds > 0:
            delay += random.uniform(0.0, self._retry_jitter_seconds)
        return max(1, int(delay))

    def retry(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempts: int,
        error_text: str,
    ) -> bool:
        delay = self._retry_delay_seconds(attempts)
        with self._connect() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE queue_jobs
                    SET
                        status = 'pending',
                        lock_owner = NULL,
                        locked_at = NULL,
                        next_attempt_at = now() + (%s || ' seconds')::interval,
                        last_error = %s,
                        updated_at = now()
                    WHERE id = %s::uuid
                      AND status = 'processing'
                      AND lock_owner = %s
                    """,
                    (
                        delay,
                        (error_text or "")[:2000],
                        job_id,
                        worker_id,
                    ),
                )
                return cur.rowcount > 0

    def move_to_dead_letter(
        self,
        *,
        job: QueueJob,
        worker_id: str,
        error_text: str,
    ) -> bool:
        with self._connect() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO queue_job_dead_letters (
                        job_id, topic, payload, dedupe_key, attempts, max_attempts, last_error
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id) DO UPDATE
                    SET
                        attempts = EXCLUDED.attempts,
                        max_attempts = EXCLUDED.max_attempts,
                        last_error = EXCLUDED.last_error,
                        created_at = now()
                    """,
                    (
                        job.id,
                        job.topic,
                        Json(job.payload),
                        job.dedupe_key,
                        int(job.attempts),
                        int(job.max_attempts),
                        (error_text or "")[:2000],
                    ),
                )
                cur.execute(
                    """
                    UPDATE queue_jobs
                    SET
                        status = 'dead',
                        lock_owner = NULL,
                        locked_at = NULL,
                        completed_at = now(),
                        last_error = %s,
                        updated_at = now()
                    WHERE id = %s::uuid
                      AND status = 'processing'
                      AND lock_owner = %s
                    """,
                    (
                        (error_text or "")[:2000],
                        job.id,
                        worker_id,
                    ),
                )
                updated = cur.rowcount > 0
                conn.commit()
                return updated

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                        COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                        COUNT(*) FILTER (WHERE status = 'done') AS done,
                        COUNT(*) FILTER (WHERE status = 'dead') AS dead,
                        COUNT(*) AS total,
                        COALESCE(
                            MAX(EXTRACT(EPOCH FROM (now() - created_at))) FILTER (WHERE status = 'pending'),
                            0
                        ) AS oldest_pending_age_seconds,
                        COALESCE(
                            COUNT(*) FILTER (
                                WHERE status = 'processing'
                                  AND locked_at < now() - (%s || ' seconds')::interval
                            ),
                            0
                        ) AS stale_processing
                    FROM queue_jobs
                    """
                    ,
                    (self._lock_ttl_seconds,),
                )
                row = cur.fetchone() or (0, 0, 0, 0, 0, 0, 0)

        return {
            "pending": int(row[0] or 0),
            "processing": int(row[1] or 0),
            "done": int(row[2] or 0),
            "dead": int(row[3] or 0),
            "total": int(row[4] or 0),
            "oldest_pending_age_seconds": int(row[5] or 0),
            "stale_processing": int(row[6] or 0),
            "timestamp": int(time.time()),
        }
