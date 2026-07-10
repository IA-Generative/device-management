"""Routes du proxy LLM OpenAI-compatible (/llm/v1) — cœur volontairement minimal.

Le plugin appende lui-même /chat/completions et /models à son llmEndpoint : ces
deux chemins constituent le contrat. Le cœur d'une requête tient en cinq étapes
(auth → pipeline pré → résolution backend → forward → pipeline post/finalize) ;
tout le reste (quota, guardrails, backends) est enfichable par configuration.

``build_router(relay_auth, hop_headers)`` reçoit les helpers de app.main par
INJECTION (pas d'import circulaire) : relay_auth = _relay_auth_from_request,
hop_headers = _RELAY_HOP_HEADERS.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable

from fastapi import APIRouter, Request, Response

from ..settings import settings
from . import audit, metrics
from .auth import LlmAuthenticator, LlmIdentity
from .backends import Backend, BackendRegistry, public_llm_proxy_url
from .errors import LlmProxyError, openai_error
from .guardrails import GuardrailInterceptor
from .pipeline import InterceptorPipeline, LlmRequestContext
from .proxy import build_outbound_headers, forward_chat_completions, forward_models
from .throttle import ThrottleInterceptor

logger = logging.getLogger("device-management.llm")


def _build_pipeline() -> InterceptorPipeline:
    # Reconstruit à chaque requête : les règles actives (guardrails, limites)
    # viennent de la config hot-reloadable. Ordre fixe documenté : throttle
    # d'abord (pas de guardrail coûteux pour un client déjà au quota), puis
    # guardrails.
    return InterceptorPipeline([ThrottleInterceptor(), GuardrailInterceptor()])


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "") or uuid.uuid4().hex


def _is_self_proxy(base_url: str) -> bool:
    """Garde anti-boucle : refuser un backend qui pointe sur le proxy lui-même."""
    public_url = public_llm_proxy_url()
    return bool(public_url) and base_url.rstrip("/").startswith(public_url.rstrip("/"))


def _make_finalize(ctx: LlmRequestContext) -> Callable[[int, str | None], None]:
    """Métriques + audit + gauge, appelé EXACTEMENT une fois par requête —
    à la vraie fin (le finally du générateur pour un stream)."""
    done = {"called": False}
    metrics.active_inc()

    def finalize(status: int, error_kind: str | None) -> None:
        if done["called"]:
            return
        done["called"] = True
        metrics.active_dec()
        duration = time.perf_counter() - ctx.t_start
        metrics.observe_request(
            route=ctx.route, model=ctx.model, backend=ctx.backend_name,
            status=status, duration_seconds=duration,
        )
        if error_kind:
            metrics.error_inc(error_kind)
        audit.log_request(ctx, status=status, duration_seconds=duration,
                          error_kind=error_kind, usage=ctx.meta.get("usage"))

    return finalize


def _resolve_backend(
    ctx: LlmRequestContext, finalize: Callable[[int, str | None], None]
) -> Backend | Response:
    backend = BackendRegistry.from_env().resolve(ctx.model)
    if backend is None:
        if ctx.model:
            finalize(404, "model_not_found")
            return openai_error(404, f"Model '{ctx.model}' has no configured backend.",
                                code="model_not_found")
        finalize(503, "no_backend")
        return openai_error(503, "No LLM backend configured.",
                            err_type="api_error", code="no_backend")
    if _is_self_proxy(backend.base_url):
        finalize(508, "backend_loop")
        return openai_error(508, "LLM backend points back to this proxy.",
                            err_type="api_error", code="backend_loop")
    ctx.backend_name = backend.name
    return backend


def build_router(
    *,
    relay_auth: Callable[..., tuple[bool, dict | str]],
    hop_headers: Iterable[str],
) -> APIRouter:
    router = APIRouter(prefix="/llm/v1", tags=["llm-proxy"])
    authenticator = LlmAuthenticator(relay_auth)

    @router.get("/models")
    async def llm_models(request: Request) -> Response:
        ctx = LlmRequestContext(
            identity=LlmIdentity("", "", "none"), trace_id=_trace_id(request), route="models"
        )
        finalize = _make_finalize(ctx)
        try:
            try:
                ctx.identity = await authenticator.resolve(request)
            except LlmProxyError as exc:
                finalize(exc.status_code, "auth_failed")
                return exc.to_response()

            backend = _resolve_backend(ctx, finalize)
            if isinstance(backend, Response):
                return backend
            headers = build_outbound_headers(request, ctx, backend, hop_headers)
            return await forward_models(ctx, backend, headers, finalize)
        except Exception:
            logger.exception("llm proxy /models failed (trace_id=%s)", ctx.trace_id)
            finalize(500, "internal_error")
            return openai_error(500, "Internal proxy error.", err_type="api_error",
                                code="internal_error")

    @router.post("/chat/completions")
    async def llm_chat_completions(request: Request) -> Response:
        ctx = LlmRequestContext(
            identity=LlmIdentity("", "", "none"),
            trace_id=_trace_id(request),
            route="chat/completions",
        )
        finalize = _make_finalize(ctx)
        try:
            try:
                ctx.identity = await authenticator.resolve(request)
            except LlmProxyError as exc:
                finalize(exc.status_code, "auth_failed")
                return exc.to_response()

            raw = await request.body()
            if len(raw) > settings.max_body_size_mb * 1024 * 1024:
                finalize(413, "body_too_large")
                return openai_error(413, "Request body too large.", code="body_too_large")
            try:
                payload = json.loads(raw or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
            except ValueError:
                finalize(400, "invalid_json")
                return openai_error(400, "Request body must be a JSON object.",
                                    code="invalid_json")

            ctx.model = str(payload.get("model") or os.getenv("DEFAULT_MODEL_NAME", "") or "")
            if ctx.model and not payload.get("model"):
                payload["model"] = ctx.model
            ctx.stream = bool(payload.get("stream"))
            ctx.payload = payload

            pipeline = _build_pipeline()
            denied = await pipeline.run_before(ctx)
            if denied is not None:
                finalize(denied.status_code, "denied")
                return denied

            backend = _resolve_backend(ctx, finalize)
            if isinstance(backend, Response):
                return backend

            return await forward_chat_completions(
                request, ctx, backend, pipeline, hop_headers, finalize
            )
        except Exception:
            logger.exception("llm proxy /chat/completions failed (trace_id=%s)", ctx.trace_id)
            finalize(500, "internal_error")
            return openai_error(500, "Internal proxy error.", err_type="api_error",
                                code="internal_error")

    return router
