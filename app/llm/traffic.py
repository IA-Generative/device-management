"""Trafic LLM bucketé en base — la source de l'« histogramme » du dashboard.

Un INSERT par requête serait intenable (l'indexation RAG = rafales de centaines
d'appels /embeddings par minute) : on agrège EN MÉMOIRE par bucket de 5 min ×
(route, model, status_class), et un flush périodique UPSERT le tout — même
famille de patterns que `llm_quota_counters` (UPSERT atomique, sûr
multi-réplicas) et que le heartbeat `config_pod_state` (écriture périodique).

Best-effort assumé : l'enregistrement ne doit JAMAIS casser une requête ; si la
base est injoignable, l'accumulateur borné abandonne les buckets les plus
anciens (on perd un point de courbe, pas une requête client).

Contrairement aux métriques Prometheus du proxy (par pod, en mémoire, perdues
au restart), ces compteurs sont multi-pods et persistants — c'est eux que lit
le dashboard admin. Les deux coexistent : Prometheus pour la latence fine
(p50/p95/p99 dans Grafana), la base pour l'usage produit.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("dm-llm-traffic")

BUCKET_SECONDS = 300          # 5 min — assez fin pour la vue 24 h du dashboard
FLUSH_INTERVAL_SECONDS = 15   # regroupe les rafales d'embeddings en 1 UPSERT/bucket
MAX_PENDING_KEYS = 1000       # garde-fou mémoire si la base est longtemps absente
PURGE_AFTER_DAYS = 90
_PURGE_EVERY_SECONDS = 6 * 3600

# clef → [count, duration_ms_sum, duration_ms_max, tokens_sum]
_acc: dict[tuple[int, str, str, str], list[int]] = {}
_lock = threading.Lock()
_flusher_started = False
_last_purge = 0.0

_ROUTE_ALIASES = {"chat/completions": "chat"}


def _status_class(status: int) -> str:
    try:
        return f"{int(status) // 100}xx"
    except Exception:
        return "0xx"


def record(*, route: str, model: str | None, status: int,
           duration_seconds: float, usage: dict | None = None) -> None:
    """Accumule une requête (appelé par le finalize du proxy — jamais fatal)."""
    try:
        r = _ROUTE_ALIASES.get(route or "", route or "?")
        bucket = int(time.time() // BUCKET_SECONDS) * BUCKET_SECONDS
        tokens = 0
        if isinstance(usage, dict):
            try:
                tokens = int(usage.get("total_tokens") or 0)
            except Exception:
                tokens = 0
        dur_ms = max(0, int(duration_seconds * 1000))
        key = (bucket, r, (model or "")[:200], _status_class(status))
        with _lock:
            row = _acc.get(key)
            if row is None:
                if len(_acc) >= MAX_PENDING_KEYS:
                    # Base absente depuis longtemps : on jette le bucket le plus
                    # ancien plutôt que de croître sans borne.
                    oldest = min(_acc)
                    _acc.pop(oldest, None)
                _acc[key] = [1, dur_ms, dur_ms, tokens]
            else:
                row[0] += 1
                row[1] += dur_ms
                row[2] = max(row[2], dur_ms)
                row[3] += tokens
        _ensure_flusher()
    except Exception:  # pragma: no cover - le trafic ne casse jamais la requête
        logger.debug("llm traffic record failed", exc_info=True)


def flush_now(conn=None) -> int:
    """UPSERT tout l'accumulateur. Retourne le nombre de lignes upsertées.

    `conn` injectable pour les tests ; sinon connexion du pool applicatif.
    En cas d'échec, les compteurs sont re-fusionnés (bornés) pour le prochain
    flush — on ne perd pas un pic de trafic sur un blip de la base.
    """
    with _lock:
        if not _acc:
            return 0
        pending = dict(_acc)
        _acc.clear()

    own_ctx = None
    try:
        if conn is None:
            from app.services.db import pooled_conn
            own_ctx = pooled_conn()
            if own_ctx is None:
                raise RuntimeError("no db pool")
            conn = own_ctx.__enter__()
        cur = conn.cursor()
        for (bucket, route, model, status_class), (n, dur_sum, dur_max, tokens) in pending.items():
            cur.execute(
                """
                INSERT INTO llm_traffic
                    (bucket_ts, route, model, status_class,
                     count, duration_ms_sum, duration_ms_max, tokens_sum)
                VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bucket_ts, route, model, status_class) DO UPDATE SET
                    count = llm_traffic.count + EXCLUDED.count,
                    duration_ms_sum = llm_traffic.duration_ms_sum + EXCLUDED.duration_ms_sum,
                    duration_ms_max = GREATEST(llm_traffic.duration_ms_max, EXCLUDED.duration_ms_max),
                    tokens_sum = llm_traffic.tokens_sum + EXCLUDED.tokens_sum
                """,
                (bucket, route, model, status_class, n, dur_sum, dur_max, tokens),
            )
        _maybe_purge(cur)
        return len(pending)
    except Exception:
        logger.debug("llm traffic flush failed — buckets re-fusionnés", exc_info=True)
        with _lock:
            for key, vals in pending.items():
                row = _acc.get(key)
                if row is None:
                    if len(_acc) < MAX_PENDING_KEYS:
                        _acc[key] = vals
                else:
                    row[0] += vals[0]
                    row[1] += vals[1]
                    row[2] = max(row[2], vals[2])
                    row[3] += vals[3]
        return 0
    finally:
        if own_ctx is not None:
            try:
                own_ctx.__exit__(None, None, None)
            except Exception:
                pass


def _maybe_purge(cur) -> None:
    """Rétention : purge opportuniste (au plus toutes les 6 h par pod).
    La table reste minuscule (buckets 5 min agrégés), c'est de l'hygiène."""
    global _last_purge
    now = time.time()
    if now - _last_purge < _PURGE_EVERY_SECONDS:
        return
    _last_purge = now
    try:
        cur.execute(
            "DELETE FROM llm_traffic WHERE bucket_ts < now() - (%s || ' days')::interval",
            (str(PURGE_AFTER_DAYS),),
        )
    except Exception:
        logger.debug("llm traffic purge failed", exc_info=True)


def _ensure_flusher() -> None:
    """Démarre (une fois) le thread de flush périodique — lazy : rien ne tourne
    tant qu'aucun trafic LLM n'est enregistré (tests et pods admin tranquilles)."""
    global _flusher_started
    if _flusher_started:
        return
    with _lock:
        if _flusher_started:
            return
        _flusher_started = True
    t = threading.Thread(target=_flusher_loop, name="llm-traffic-flush", daemon=True)
    t.start()


def _flusher_loop() -> None:  # pragma: no cover - boucle infinie, testée via flush_now
    while True:
        time.sleep(FLUSH_INTERVAL_SECONDS)
        try:
            flush_now()
        except Exception:
            logger.debug("llm traffic flusher iteration failed", exc_info=True)
