"""Métriques Prometheus du proxy LLM (registry dédié, concaténé dans /metrics).

Histogrammes → percentiles de latence (p50/p95/p99 via histogram_quantile côté
Prometheus). Registry DÉDIÉ (pas le global) : on n'exporte que nos métriques,
sans le bruit python_gc_* — cohérent avec le /metrics fait-main existant auquel
render() est concaténé. Mono-process uvicorn → pas de mode multiprocess requis.

prometheus_client est une dépendance déclarée ; le fallback no-op ne sert qu'à
ne pas casser un environnement dégradé (lib absente).
"""
from __future__ import annotations

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _HAS_PROMETHEUS = True
except ModuleNotFoundError:  # pragma: no cover - dépendance déclarée dans requirements
    _HAS_PROMETHEUS = False

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0)

if _HAS_PROMETHEUS:
    REGISTRY = CollectorRegistry()
    REQUESTS = Counter(
        "dm_llm_requests_total",
        "Requêtes traitées par le proxy LLM.",
        ["route", "model", "backend", "status"],
        registry=REGISTRY,
    )
    DURATION = Histogram(
        "dm_llm_request_duration_seconds",
        "Latence de bout en bout des requêtes proxy LLM (streaming inclus).",
        ["route", "backend"],
        buckets=_LATENCY_BUCKETS,
        registry=REGISTRY,
    )
    ERRORS = Counter(
        "dm_llm_errors_total",
        "Erreurs du proxy LLM par catégorie.",
        ["kind"],
        registry=REGISTRY,
    )
    ACTIVE = Gauge(
        "dm_llm_active_requests",
        "Requêtes LLM en cours (streams ouverts inclus).",
        registry=REGISTRY,
    )
    QUOTA_DENIED = Counter(
        "dm_llm_quota_denied_total",
        "Requêtes refusées pour dépassement de quota (429).",
        registry=REGISTRY,
    )


def observe_request(*, route: str, model: str, backend: str, status: int, duration_seconds: float) -> None:
    if not _HAS_PROMETHEUS:
        return
    REQUESTS.labels(route=route, model=model or "unknown", backend=backend or "unknown",
                    status=str(status)).inc()
    DURATION.labels(route=route, backend=backend or "unknown").observe(max(0.0, duration_seconds))


def error_inc(kind: str) -> None:
    if _HAS_PROMETHEUS:
        ERRORS.labels(kind=kind).inc()


def quota_denied_inc() -> None:
    if _HAS_PROMETHEUS:
        QUOTA_DENIED.inc()


def active_inc() -> None:
    if _HAS_PROMETHEUS:
        ACTIVE.inc()


def active_dec() -> None:
    if _HAS_PROMETHEUS:
        ACTIVE.dec()


def render() -> str:
    """Texte Prometheus du registry LLM, à concaténer au /metrics existant."""
    if not _HAS_PROMETHEUS:
        return ""
    return generate_latest(REGISTRY).decode("utf-8")
