"""Observabilité cloud-native : request-id de corrélation, logs JSON, OpenTelemetry.

Tout est désactivé par défaut (comportement air-gapped inchangé) :
  - Le format JSON des logs applicatifs est opt-in via ``DM_LOG_FORMAT=json``
    (sinon, format texte historique, désormais enrichi du request_id).
  - OpenTelemetry ne s'active que si ``OTEL_EXPORTER_OTLP_ENDPOINT`` est défini ;
    ``configure_otel()`` est un no-op sinon (coût zéro en air-gapped).
Le request-id (en-tête ``X-Request-ID``, honoré si fourni sinon généré) est lui
toujours actif : il ne dépend d'aucun service externe et sert de fil rouge
entre les logs applicatifs et les traces OTel.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("dm_request_id", default="")


def current_request_id() -> str:
    """Return the request id of the request currently being processed (or "")."""
    return _REQUEST_ID.get()


class RequestIdLogFilter(logging.Filter):
    """Injects the current request id into every LogRecord as `request_id`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


class JsonLogFormatter(logging.Formatter):
    """Minimal, dependency-free JSON log formatter for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Assign a correlation id to the request: honor `X-Request-ID` if the
    caller supplied one, generate one otherwise. Always returned in the
    response header, always injected into log records via `RequestIdLogFilter`,
    and attached to the current OTel span (best-effort, no-op if tracing off)."""
    request_id = (request.headers.get("x-request-id") or "").strip() or uuid.uuid4().hex
    token = _REQUEST_ID.set(request_id)
    request.state.request_id = request_id
    try:
        try:
            from opentelemetry import trace

            trace.get_current_span().set_attribute("dm.request_id", request_id)
        except Exception:
            pass
        response = await call_next(request)
    finally:
        _REQUEST_ID.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


def configure_otel(app) -> bool:
    """Enable OpenTelemetry tracing (FastAPI + httpx + psycopg2) when
    OTEL_EXPORTER_OTLP_ENDPOINT is set. No-op (returns False) otherwise, so
    air-gapped deployments pay zero cost and need no collector."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.getenv("OTEL_SERVICE_NAME", "device-management")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    Psycopg2Instrumentor().instrument()
    return True
