"""Adresse de l'appelant sur /config (issue #54) : l'adresse vérifiée prime sur l'en-tête."""
from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def mod():
    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    os.environ.setdefault("DM_CONFIG_ENABLED", "true")
    if "app.main" in sys.modules:
        return sys.modules["app.main"]
    return importlib.import_module("app.main")


def _request(**headers):
    return SimpleNamespace(headers={k.replace("_", "-"): v for k, v in headers.items()})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DM_REQUIRE_VERIFIED_EMAIL", raising=False)


def test_verified_email_wins_over_the_header(mod):
    request = _request(X_User_Email="usurpee@example.test")
    assert mod._caller_email(request, True, {"email": "Vraie@Example.test"}) == ("Vraie@Example.test", True)


def test_header_is_only_a_fallback_without_relay_credentials(mod):
    request = _request(X_User_Email=" declaree@example.test ")
    assert mod._caller_email(request, False, "missing relay headers") == ("declaree@example.test", False)
    assert mod._caller_email(_request(), False, None) == ("", False)
    # Paire relais valide mais sans adresse connue : l'en-tête reste le repli.
    assert mod._caller_email(request, True, {"email": ""}) == ("declaree@example.test", False)


def test_strict_mode_ignores_an_unverified_header(mod, monkeypatch):
    monkeypatch.setenv("DM_REQUIRE_VERIFIED_EMAIL", "true")
    request = _request(X_User_Email="declaree@example.test")
    assert mod._caller_email(request, False, None) == ("", False)
    assert mod._caller_email(request, True, {"email": "vraie@example.test"}) == ("vraie@example.test", True)


def test_relay_check_is_done_when_the_caller_did_not(mod, monkeypatch):
    calls = []

    def fake(request, *, target=None, require_proxy_token=False):
        calls.append(target)
        return True, {"email": "vraie@example.test"}

    monkeypatch.setattr(mod, "_relay_auth_from_request", fake)
    assert mod._caller_email(_request(X_User_Email="autre@example.test")) == ("vraie@example.test", True)
    assert calls == ["config"]
