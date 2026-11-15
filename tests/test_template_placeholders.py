"""Repères `${{VAR}}` d'un gabarit de configuration : jamais un secret du serveur.

Le gabarit vient d'un paquet de plugin. Sans garde, un repère `${{DATABASE_URL}}`
dans une clé quelconque faisait servir la valeur à tous les postes.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture(scope="module")
def mod():
    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    return sys.modules.get("app.main") or importlib.import_module("app.main")


def test_a_placeholder_never_resolves_a_server_secret(mod, monkeypatch):
    for name, value in (("DATABASE_URL", "postgresql://secret"), ("DM_PACKAGE_SIGNING_KEY", "cle"),
                        ("ADMIN_SESSION_SECRET", "session"), ("DM_GITHUB_TOKEN", "jeton")):
        monkeypatch.setenv(name, value)
        assert mod._substitute_env_in_str("x=${{" + name + "}}") == "x=", name
        assert mod._substitute_env_in_str("x=${" + name + "}") == "x=", name


def test_governed_secrets_and_plain_settings_are_still_resolved(mod, monkeypatch):
    monkeypatch.setenv("LLM_API_TOKEN", "jeton-llm")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "client")
    monkeypatch.setenv("UN_REGLAGE_ORDINAIRE", "valeur")
    assert mod._substitute_env_in_str("${{LLM_API_TOKEN}}|${{KEYCLOAK_CLIENT_ID}}|${{UN_REGLAGE_ORDINAIRE}}") \
        == "jeton-llm|client|valeur"
    assert mod._substitute_env_in_str("${{INCONNUE_DU_SERVEUR}}") == ""


def test_telemetry_tokens_of_the_documented_templates_are_still_resolved(mod, monkeypatch):
    """Le chart les décrit comme des repères de gabarit : les vider couperait la télémétrie des postes."""
    monkeypatch.setenv("TELEMETRY_KEY", "cle-de-gabarit")
    monkeypatch.setenv("TELEMETRY_SALT", "sel-de-gabarit")
    assert mod._substitute_env_in_str("${{TELEMETRY_KEY}}|${{TELEMETRY_SALT}}") == "cle-de-gabarit|sel-de-gabarit"
    assert mod._substitute_env_in_str("${TELEMETRY_SALT}") == "sel-de-gabarit"
