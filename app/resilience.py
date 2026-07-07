"""Retries bornées (backoff exponentiel + jitter) pour les appels réseau
sortants transitoires — JAMAIS pour les erreurs 4xx / de validation.

N'est appliqué qu'aux opérations idempotentes (GET, ou POST dont l'échec ne
peut pas avoir d'effet de bord partiel côté serveur) : un simple
"ça a peut-être déjà été traité" suffit à exclure un appel de ce module — voir
les commentaires aux points d'appel (ex. l'échange de code OIDC, POST à
usage unique, n'est volontairement PAS retenté ici).
"""

from __future__ import annotations

import socket
from urllib import error as urllib_error

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

try:
    # PyJWKClient wraps URLError/TimeoutError into PyJWKClientConnectionError —
    # still a transient connectivity failure, not a validation error.
    from jwt.exceptions import PyJWKClientConnectionError
except ImportError:  # pragma: no cover
    PyJWKClientConnectionError = ()  # type: ignore[assignment]


def _is_transient_httpx_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.ReadError | httpx.RemoteProtocolError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _is_transient_network_error(exc: BaseException) -> bool:
    """True for connect/timeout errors — never for 4xx or parsing/validation errors."""
    if isinstance(exc, TimeoutError | ConnectionError | socket.timeout):
        return True
    if isinstance(exc, urllib_error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib_error.URLError):
        return True
    if PyJWKClientConnectionError and isinstance(exc, PyJWKClientConnectionError):
        return True
    return _is_transient_httpx_error(exc)


def retry_transient(*, attempts: int = 3):
    """Décorateur : jusqu'à `attempts` tentatives, backoff exponentiel + jitter,
    uniquement sur erreur réseau transitoire (connect/timeout/5xx). Toute autre
    exception (4xx, erreur de validation, etc.) remonte immédiatement."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        retry=retry_if_exception(_is_transient_network_error),
    )
