"""Throttling par utilisateur — compteurs PARTAGÉS entre réplicas (PostgreSQL).

Sémantique : fenêtre fixe. Chaque requête incrémente atomiquement la ligne
(subject, window_start) via un unique UPSERT … RETURNING — cohérent entre N
réplicas sans affinité de session (les requêtes refusées comptent aussi,
comportement standard du fixed-window). Dépassement → 429 {"error", "retry_after"}.

La LIMITE est de la configuration, pas de la donnée : LLM_QUOTA_REQUESTS_PER_MINUTE
(et LLM_QUOTA_WINDOW_SECONDS) sont relues à CHAQUE requête via runtime_config.cfg()
→ éditables à chaud dans l'onglet Config admin, ≤ 0 = désactivé (défaut).

``QuotaStore`` est l'abstraction : PostgresQuotaStore aujourd'hui (conforme
ADR-0001, zéro nouvelle infra), un RedisQuotaStore demain si le débit l'exige,
sans toucher au cœur ni à l'intercepteur.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from abc import ABC, abstractmethod

import anyio.to_thread
from fastapi.responses import JSONResponse

from .. import runtime_config
from . import metrics
from .errors import openai_error
from .pipeline import Interceptor, LlmRequestContext

logger = logging.getLogger("device-management.llm")

_PURGE_INTERVAL_SECONDS = 60.0
_PURGE_RETENTION_SQL = "window_start < now() - interval '1 hour'"


def _get_psycopg2():
    try:
        import psycopg2  # noqa: PLC0415
        return psycopg2
    except ModuleNotFoundError:  # pragma: no cover
        return None


class QuotaStore(ABC):
    @abstractmethod
    def incr(self, subject: str, *, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        """Incrémente le compteur du sujet dans la fenêtre courante.

        Retourne (allowed, count_courant, retry_after_seconds). DOIT être
        atomique vis-à-vis d'appels concurrents depuis plusieurs réplicas.
        """


class PostgresQuotaStore(QuotaStore):
    """Fenêtre fixe sur table llm_quota_counters — un aller-retour DB par requête."""

    _INCR_SQL = """
        INSERT INTO llm_quota_counters AS q (subject, window_start, count)
        VALUES (
            %(subject)s,
            to_timestamp(floor(extract(epoch FROM now()) / %(win)s) * %(win)s),
            1
        )
        ON CONFLICT (subject, window_start)
        DO UPDATE SET count = q.count + 1, updated_at = now()
        RETURNING count,
            CEIL((floor(extract(epoch FROM now()) / %(win)s) + 1) * %(win)s
                 - extract(epoch FROM now()))::int
    """

    def __init__(self):
        self._last_purge = 0.0
        self._purge_lock = threading.Lock()

    def incr(self, subject: str, *, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        psycopg2 = _get_psycopg2()
        from ..services.db import db_url_bootstrap  # noqa: PLC0415
        db_url = db_url_bootstrap()
        if psycopg2 is None or not db_url:
            raise RuntimeError("PostgresQuotaStore requires psycopg2 + DATABASE_URL")
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(self._INCR_SQL, {"subject": subject, "win": int(window_seconds)})
                row = cur.fetchone()
                count = int(row[0])
                retry_after = max(1, int(row[1]))
                self._maybe_purge(cur)
            return count <= limit, count, retry_after
        finally:
            conn.close()

    def _maybe_purge(self, cur) -> None:
        # Purge opportuniste des fenêtres échues (≤ 1×/min/pod) — pas de cron.
        now = time.monotonic()
        with self._purge_lock:
            if now - self._last_purge < _PURGE_INTERVAL_SECONDS:
                return
            self._last_purge = now
        try:
            cur.execute(f"DELETE FROM llm_quota_counters WHERE {_PURGE_RETENTION_SQL}")  # nosec B608: fragment constant
        except Exception:
            logger.debug("llm quota purge skipped", exc_info=True)


class MemoryQuotaStore(QuotaStore):
    """Fallback mono-processus (dev/tests sans DB) — même sémantique fixed-window."""

    def __init__(self):
        self._counters: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def incr(self, subject: str, *, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        now = time.time()
        window_index = int(now // window_seconds)
        with self._lock:
            key = (subject, window_index)
            self._counters[key] = self._counters.get(key, 0) + 1
            count = self._counters[key]
            # Purge lazy des fenêtres passées.
            for k in [k for k in self._counters if k[0] == subject and k[1] < window_index]:
                del self._counters[k]
        retry_after = max(1, int(math.ceil((window_index + 1) * window_seconds - now)))
        return count <= limit, count, retry_after


_store: QuotaStore | None = None
_store_lock = threading.Lock()


def get_quota_store() -> QuotaStore:
    """Postgres si disponible (état partagé multi-réplicas), sinon mémoire."""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            from ..services.db import db_url_bootstrap  # noqa: PLC0415
            if _get_psycopg2() is not None and db_url_bootstrap():
                _store = PostgresQuotaStore()
            else:
                logger.info("LLM quota store: fallback mémoire (pas de DB) — mono-réplica seulement")
                _store = MemoryQuotaStore()
    return _store


def reset_quota_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None


def _quota_limit() -> int:
    try:
        return int(runtime_config.cfg("LLM_QUOTA_REQUESTS_PER_MINUTE", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _quota_window_seconds() -> int:
    try:
        window = int(runtime_config.cfg("LLM_QUOTA_WINDOW_SECONDS", 60) or 60)
    except (TypeError, ValueError):
        window = 60
    return max(10, window)


class ThrottleInterceptor(Interceptor):
    name = "throttle"

    def __init__(self, store: QuotaStore | None = None):
        self._store = store

    async def before(self, ctx: LlmRequestContext) -> JSONResponse | None:
        limit = _quota_limit()  # relu à chaque requête → hot-reload
        if limit <= 0:
            return None
        window = _quota_window_seconds()
        subject = ctx.identity.client_uuid or ctx.identity.email
        if not subject:
            return None
        store = self._store or get_quota_store()
        try:
            allowed, count, retry_after = await anyio.to_thread.run_sync(
                lambda: store.incr(subject, limit=limit, window_seconds=window)
            )
        except Exception:
            # Fail-open assumé : un incident de store de quota ne doit pas couper
            # le service LLM (le quota est une protection, pas une fonction vitale).
            logger.exception("LLM quota store error (fail-open)")
            return None
        ctx.meta["quota"] = {"count": count, "limit": limit, "window_seconds": window}
        if not allowed:
            metrics.quota_denied_inc()
            ctx.verdicts.append("throttle:deny")
            return openai_error(
                429,
                f"Rate limit exceeded: {limit} requests per {window}s window. "
                f"Retry after {retry_after}s.",
                err_type="rate_limit_exceeded",
                code="rate_limit_exceeded",
                retry_after=retry_after,
            )
        return None
