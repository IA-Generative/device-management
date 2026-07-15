"""Client httpx sortant PARTAGÉ du proxy LLM (pool keep-alive réutilisé).

Un seul AsyncClient par process : réutilisation des connexions/TLS vers le
backend (handshake amorti), limites de pool bornées (backpressure), timeouts
par défaut sûrs. ``trust_env=True`` (proxy corporate via HTTPS_PROXY) et respect
de DM_TLS_VERIFY, comme les autres appels sortants du DM.

En streaming, le timeout ``read`` s'applique ENTRE deux chunks (pas à la durée
totale du stream) — c'est le bon réglage SSE, ajustable à chaud via
DM_LLM_READ_TIMEOUT_SECONDS.
"""
from __future__ import annotations

import os
import threading

import httpx

from .. import runtime_config

DEFAULT_READ_TIMEOUT_SECONDS = 120

_client: httpx.AsyncClient | None = None
_client_lock = threading.Lock()
_test_transport: httpx.AsyncBaseTransport | None = None


def _tls_verify() -> bool:
    return os.getenv("DM_TLS_VERIFY", "true").strip().lower() not in ("false", "0", "no", "off")


def read_timeout_seconds() -> float:
    try:
        value = float(runtime_config.cfg("DM_LLM_READ_TIMEOUT_SECONDS", DEFAULT_READ_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        value = float(DEFAULT_READ_TIMEOUT_SECONDS)
    return max(10.0, value)


def request_timeout() -> httpx.Timeout:
    """Timeout par requête (relit la config → hot-reload sans recréer le client)."""
    return httpx.Timeout(connect=5.0, read=read_timeout_seconds(), write=30.0, pool=10.0)


def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                transport=_test_transport,
                trust_env=True,
                verify=_tls_verify(),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                timeout=request_timeout(),
            )
    return _client


async def aclose_async_client() -> None:
    global _client
    client = _client
    _client = None
    if client is not None and not client.is_closed:
        await client.aclose()


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """Injecte un transport factice (httpx.MockTransport) et invalide le client."""
    global _client, _test_transport
    _test_transport = transport
    _client = None
