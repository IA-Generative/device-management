"""Client httpx sortant partagé du relais d'outils (pool keep-alive, trust_env pour le
proxy de sortie, DM_TLS_VERIFY). Le délai est fixé PAR APPEL d'après le registre."""
from __future__ import annotations

import os
import threading

import httpx

_client: httpx.AsyncClient | None = None
_lock = threading.Lock()
_test_transport: httpx.AsyncBaseTransport | None = None


def _tls_verify() -> bool:
    return os.getenv("DM_TLS_VERIFY", "true").strip().lower() not in ("false", "0", "no", "off")


def timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=5.0, read=max(1.0, seconds), write=10.0, pool=5.0)


def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                transport=_test_transport,
                trust_env=True,
                verify=_tls_verify(),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
                timeout=timeout(15.0),
            )
    return _client


async def aclose_async_client() -> None:
    global _client
    client = _client
    _client = None
    if client is not None and not client.is_closed:
        await client.aclose()


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _client, _test_transport
    _test_transport = transport
    _client = None
