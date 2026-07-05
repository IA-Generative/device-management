"""Shared test fixtures — ensure env isolation between test modules."""

import os
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def _pristine_env():
    """Snapshot, une seule fois, de l'environnement pristine (avant tout test)."""
    return dict(os.environ)


@pytest.fixture(scope="module", autouse=True)
def _restore_env_per_module(_pristine_env):
    """Restaure l'environnement à l'état pristine à la fin de CHAQUE module.

    Plusieurs modules configurent l'environnement via des fixtures
    ``scope="module"`` (ex. ``mod``/``_setup_env``) qui s'initialisent AVANT le
    fixture autouse de scope fonction — une isolation par-test capturerait donc
    la pollution comme « baseline ». On isole donc au grain MODULE : l'env est
    partagé entre les tests d'un même fichier (comportement voulu par les
    fixtures module) puis remis à pristine avant le module suivant, ce qui
    neutralise la pollution inter-fichiers sans casser la persistance interne.
    """
    yield
    os.environ.clear()
    os.environ.update(_pristine_env)


@pytest.fixture(autouse=True)
def _restore_psycopg2():
    """Des tests injectent un faux module ``psycopg2`` — on le restaure par test."""
    saved = sys.modules.get("psycopg2")
    yield
    if saved is None:
        sys.modules.pop("psycopg2", None)
    else:
        sys.modules["psycopg2"] = saved


# Fichiers de tests nécessitant une infra live (Postgres/Keycloak/serveur/
# navigateur). Marqués `integration` ici, de façon centralisée, pour être
# exclus du gate CI unitaire (`pytest -m 'not integration'`).
_INTEGRATION_FILES = {
    "test_e2e_deployment.py",
    "test_post_deploy.py",
    "test_admin_playwright.py",
    "test_admin_ui.py",
}

# Tests isolés (dans des fichiers par ailleurs unitaires) qui nécessitent un
# vrai Postgres : ils écrivent/lisent réellement en base et lèvent
# OperationalError sans serveur. Marqués integration au cas par cas.
_INTEGRATION_TESTS = {
    "test_enroll_returns_relay_credentials_after_pkce",
    "test_queue_stats_requires_admin_token",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.name in _INTEGRATION_FILES or item.name in _INTEGRATION_TESTS:
            item.add_marker(pytest.mark.integration)
