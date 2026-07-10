"""Forward HTTP vers le backend LLM — non-streamé et passthrough SSE.

Règles :
- La clé backend est injectée CÔTÉ SERVEUR (Authorization sortante) et les
  credentials entrants (Authorization du client, X-Relay-*) ne sont JAMAIS
  forwardés au backend.
- ``stream: true`` → passthrough chunk par chunk (aiter_raw → StreamingResponse),
  AUCUNE bufferisation : la backpressure du client se propage à l'upstream, la
  mémoire par stream est constante, la connexion upstream est toujours fermée
  (finally) même si le client déconnecte.
- ``accept-encoding: identity`` vers le backend : les chunks restent inspectables
  par les hooks on_chunk (pas de gzip opaque).
- Erreurs mappées en statuts exploitables : 502 backend injoignable, 504 timeout,
  passthrough des statuts d'erreur du backend.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .backends import Backend
from .errors import LlmProxyError, openai_error
from .http_client import get_async_client, request_timeout
from .pipeline import InterceptorPipeline, LlmRequestContext

# En-têtes entrants à ne jamais forwarder (credentials, encodage, transport ;
# x-request-id entrant remplacé par le trace_id canonique posé plus bas).
_STRIP_INBOUND_HEADERS = frozenset({
    "authorization", "cookie", "x-relay-client", "x-relay-key",
    "x-client-id", "x-client-key", "x-relay-proxy-token",
    "accept-encoding", "content-length", "content-type", "x-request-id",
})

# FinalizeFn(status_code, error_kind) — métriques + audit, appelé EXACTEMENT une
# fois par requête (dans le finally du générateur pour les streams).
FinalizeFn = Callable[[int, str | None], None]


def build_outbound_headers(
    request: Request,
    ctx: LlmRequestContext,
    backend: Backend,
    hop_headers: Iterable[str],
) -> dict[str, str]:
    hop = {h.lower() for h in hop_headers}
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_INBOUND_HEADERS and k.lower() not in hop
    }
    if backend.api_token:
        headers["Authorization"] = f"Bearer {backend.api_token}"
    headers["Accept-Encoding"] = "identity"
    headers["X-Request-Id"] = ctx.trace_id
    return headers


def _map_transport_error(exc: httpx.HTTPError) -> LlmProxyError:
    if isinstance(exc, httpx.TimeoutException):
        return LlmProxyError(504, "LLM backend timed out.", err_type="api_error",
                             code="backend_timeout")
    return LlmProxyError(502, "LLM backend unreachable.", err_type="api_error",
                         code="backend_unreachable")


def _passthrough_response(resp: httpx.Response) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


async def forward_models(
    ctx: LlmRequestContext,
    backend: Backend,
    headers: dict[str, str],
    finalize: FinalizeFn,
) -> Response:
    client = get_async_client()
    try:
        resp = await client.get(
            f"{backend.base_url}/models", headers=headers, timeout=request_timeout()
        )
    except httpx.HTTPError as exc:
        err = _map_transport_error(exc)
        finalize(err.status_code, err.code)
        return err.to_response()
    finalize(resp.status_code, None if resp.status_code < 400 else "backend_error")
    return _passthrough_response(resp)


async def forward_chat_completions(
    request: Request,
    ctx: LlmRequestContext,
    backend: Backend,
    pipeline: InterceptorPipeline,
    hop_headers: Iterable[str],
    finalize: FinalizeFn,
) -> Response:
    client = get_async_client()
    headers = build_outbound_headers(request, ctx, backend, hop_headers)
    url = f"{backend.base_url}/chat/completions"

    if not ctx.stream:
        try:
            resp = await client.post(
                url, json=ctx.payload, headers=headers, timeout=request_timeout()
            )
        except httpx.HTTPError as exc:
            err = _map_transport_error(exc)
            finalize(err.status_code, err.code)
            return err.to_response()

        if resp.status_code >= 400:
            finalize(resp.status_code, "backend_error")
            return _passthrough_response(resp)

        content_type = resp.headers.get("content-type", "")
        if "application/json" not in content_type:
            finalize(resp.status_code, None)
            return _passthrough_response(resp)

        try:
            body = resp.json()
        except ValueError:
            finalize(502, "backend_invalid_json")
            return openai_error(502, "LLM backend returned invalid JSON.",
                                err_type="api_error", code="backend_invalid_json")
        try:
            body = await pipeline.run_after(ctx, body)
        except LlmProxyError as exc:
            finalize(exc.status_code, exc.code)
            return exc.to_response()
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            ctx.meta["usage"] = body["usage"]
        finalize(resp.status_code, None)
        return JSONResponse(body, status_code=resp.status_code)

    # ── Streaming SSE : passthrough token par token, zéro bufferisation ──
    outbound = client.build_request(
        "POST", url, json=ctx.payload, headers=headers, timeout=request_timeout()
    )
    try:
        resp = await client.send(outbound, stream=True)
    except httpx.HTTPError as exc:
        err = _map_transport_error(exc)
        finalize(err.status_code, err.code)
        return err.to_response()

    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        finalize(resp.status_code, "backend_error")
        return Response(
            content=body,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    async def body_iterator():
        error_kind: str | None = None
        try:
            async for chunk in pipeline.wrap_stream(ctx, resp.aiter_raw()):
                yield chunk
        except httpx.HTTPError:
            # Upstream coupé en plein stream : le flux s'arrête, l'audit le note.
            error_kind = "backend_stream_interrupted"
        finally:
            await resp.aclose()
            finalize(resp.status_code, error_kind)

    return StreamingResponse(
        body_iterator(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "text/event-stream"),
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",   # nginx : ne pas bufferiser le SSE
            "X-Request-Id": ctx.trace_id,
        },
    )
