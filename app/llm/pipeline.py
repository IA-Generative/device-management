"""Pipeline d'intercepteurs du proxy LLM (chain-of-responsibility).

Le cœur du proxy ne connaît que : run_before → forward → run_after/wrap_stream.
Toute règle (guardrail, throttling, …) est un Interceptor ajouté/retiré/réordonné
par CONFIGURATION — jamais en modifiant le cœur.

Contrat :
- ``before(ctx)``  : hook PRÉ-REQUÊTE. Retourner une JSONResponse court-circuite
  (deny) ; retourner None continue. Peut muter ctx.payload (transform).
- ``after(ctx, body)`` : hook POST-RÉPONSE (réponse non-streamée complète).
  Retourne le body (éventuellement transformé) ; lever LlmProxyError = deny.
- ``on_chunk(ctx, chunk)`` : hook POST-RÉPONSE en streaming, best-effort par
  chunk (les chunks bruts ne sont pas alignés sur les événements SSE ; une
  inspection sémantique complète exigerait de bufferiser, contraire au
  passthrough — limite assumée et documentée).
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse

from .auth import LlmIdentity


@dataclass
class LlmRequestContext:
    identity: LlmIdentity
    trace_id: str
    route: str                    # "chat/completions" | "models"
    model: str = ""
    stream: bool = False
    payload: dict | None = None   # body JSON entrant (mutable par transform)
    backend_name: str = ""
    t_start: float = field(default_factory=time.perf_counter)
    verdicts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class Interceptor:
    """Base no-op : toute règle future hérite et surcharge ce qui la concerne."""

    name = "interceptor"

    async def before(self, ctx: LlmRequestContext) -> JSONResponse | None:
        return None

    async def after(self, ctx: LlmRequestContext, body: dict) -> dict:
        return body

    def on_chunk(self, ctx: LlmRequestContext, chunk: bytes) -> bytes:
        return chunk


class InterceptorPipeline:
    def __init__(self, interceptors: list[Interceptor]):
        self.interceptors = list(interceptors)

    async def run_before(self, ctx: LlmRequestContext) -> JSONResponse | None:
        for interceptor in self.interceptors:
            response = await interceptor.before(ctx)
            if response is not None:
                ctx.verdicts.append(f"{interceptor.name}:deny")
                return response
        return None

    async def run_after(self, ctx: LlmRequestContext, body: dict) -> dict:
        for interceptor in self.interceptors:
            body = await interceptor.after(ctx, body)
        return body

    async def wrap_stream(
        self, ctx: LlmRequestContext, aiter: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        async for chunk in aiter:
            for interceptor in self.interceptors:
                chunk = interceptor.on_chunk(ctx, chunk)
            yield chunk
