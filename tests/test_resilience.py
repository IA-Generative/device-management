"""Cloud-native readiness — retries bornées (backoff + jitter) sur les appels
réseau transitoires (JWKS/Keycloak, LLM), jamais sur une erreur 4xx."""

import urllib.error

import httpx
import pytest

from app import resilience


def test_retries_on_connection_error_then_succeeds():
    calls = {"n": 0}

    @resilience.retry_transient()
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection reset")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_gives_up_after_max_attempts():
    calls = {"n": 0}

    @resilience.retry_transient(attempts=3)
    def always_fails():
        calls["n"] += 1
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        always_fails()
    assert calls["n"] == 3


def test_does_not_retry_client_errors():
    calls = {"n": 0}

    @resilience.retry_transient()
    def bad_request():
        calls["n"] += 1
        raise ValueError("not a transient error")

    with pytest.raises(ValueError):
        bad_request()
    assert calls["n"] == 1  # no retry — fails fast


def test_does_not_retry_http_4xx():
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert resilience._is_transient_network_error(exc) is False


def test_retries_http_5xx():
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("unavailable", request=request, response=response)
    assert resilience._is_transient_network_error(exc) is True


def test_urllib_httperror_4xx_not_transient():
    exc = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)
    assert resilience._is_transient_network_error(exc) is False


def test_urllib_httperror_5xx_is_transient():
    exc = urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, None)
    assert resilience._is_transient_network_error(exc) is True


def test_retry_transient_recovers_from_503_then_succeeds():
    """End-to-end: the decorator itself retries a 503 HTTPStatusError and
    returns the eventual success — not just the classifier in isolation."""
    calls = {"n": 0}
    request = httpx.Request("GET", "https://example.com")

    @resilience.retry_transient()
    def flaky_upstream():
        calls["n"] += 1
        if calls["n"] < 2:
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("service unavailable", request=request, response=response)
        return "ok"

    assert flaky_upstream() == "ok"
    assert calls["n"] == 2


def test_retry_transient_does_not_retry_404():
    calls = {"n": 0}
    request = httpx.Request("GET", "https://example.com")

    @resilience.retry_transient()
    def not_found():
        calls["n"] += 1
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        not_found()
    assert calls["n"] == 1
