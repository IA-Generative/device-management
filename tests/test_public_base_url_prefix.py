"""PUBLIC_BASE_URL avec préfixe de chemin (ingress non-racine, ex. /bootstrap).

Régression DGX : le service n'existe QUE sous https://host/bootstrap ; les
endpoints diffusés par /config doivent conserver le chemin de PUBLIC_BASE_URL —
SAUF telemetryEndpoint, servi à la RACINE de l'origine car le plugin re-base
lui-même le path sur bootstrapUrl (préfixe compris) : le préfixer côté DM
doublait /bootstrap (404). Un déploiement À LA RACINE doit produire exactement
les URLs historiques (non-régression).
"""
import importlib
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

_TEST_CONFIG = {
    "configVersion": 1,
    "config": {
        "llm_base_urls": "${{LLM_BASE_URL}}",
        "llm_api_tokens": "${{LLM_API_TOKEN}}",
        "authHeaderName": "Authorization",
        "authHeaderPrefix": "Bearer ",
        "enabled": True,
        "telemetryEnabled": True,
        "telemetrylogJson": True,
    },
}

_config_dir = None


def _ensure_config_fixture():
    global _config_dir
    if _config_dir and os.path.isdir(_config_dir):
        return _config_dir
    _config_dir = tempfile.mkdtemp(prefix="dm-test-prefix-config-")
    lo_dir = os.path.join(_config_dir, "libreoffice")
    os.makedirs(lo_dir, exist_ok=True)
    for d in (_config_dir, lo_dir):
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump(_TEST_CONFIG, f)
        with open(os.path.join(d, "config.prod.json"), "w") as f:
            json.dump(_TEST_CONFIG, f)
    return _config_dir


def _load_module(public_base_url: str):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_CONFIG_DIR"] = _ensure_config_fixture()
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_RELAY_SECRET_PEPPER"] = "unit-test-pepper"
    # keycloakTokenEndpoint n'est diffusé que si le forçage relais est actif.
    os.environ["DM_RELAY_FORCE_KEYCLOAK_ENDPOINTS"] = "true"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["LLM_BASE_URL"] = "https://backend.test/v1"
    os.environ["LLM_API_TOKEN"] = "secret-token-test"
    os.environ["PUBLIC_BASE_URL"] = public_base_url
    # llmEndpoint doit se DÉRIVER de PUBLIC_BASE_URL ; telemetryEndpoint du
    # défaut relatif — purger toute pollution inter-modules.
    for key in ("PUBLIC_LLM_PROXY_URL", "FORCE_LLM_ENDPOINT_OVERRIDE",
                "TELEMETRY_PUBLIC_ENDPOINT", "DM_TELEMETRY_PUBLIC_ENDPOINT",
                "DM_BOOTSTRAP_URLS", "DATABASE_URL", "DATABASE_ADMIN_URL"):
        os.environ.pop(key, None)

    sys.modules.pop("psycopg2", None)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def _get_config(mod):
    client = TestClient(mod.app)
    res = client.get("/config/libreoffice/config.json?profile=prod")
    assert res.status_code == 200, res.text
    return res.json()["config"]


def test_all_advertised_endpoints_keep_ingress_path_prefix():
    """PUBLIC_BASE_URL=https://host/bootstrap → endpoints re-basés par le DM
    avec le préfixe d'ingress — SAUF telemetryEndpoint (cf. test dédié)."""
    cfg = _get_config(_load_module("https://host/bootstrap"))

    for key in ("keycloakTokenEndpoint", "relayAssistantBaseUrl", "llmEndpoint"):
        value = cfg.get(key, "")
        assert value.startswith("https://host/bootstrap"), f"{key} = {value!r}"


def test_telemetry_endpoint_served_at_origin_root_behind_prefixed_ingress():
    """telemetryEndpoint relatif = RACINE de l'origine, SANS le BASE_PATH.

    C'est le plugin (telemetry.js _resolveEndpoint) qui re-base le path sur
    bootstrapUrl (préfixe d'ingress compris) : si le DM préfixe AUSSI, le
    /bootstrap est compté deux fois → POST .../bootstrap/bootstrap/telemetry/
    v1/traces → 404 (constaté DGX). Le plugin re-baseur reconstruit ensuite
    https://host/bootstrap/telemetry/v1/traces — une seule fois."""
    cfg = _get_config(_load_module("https://host/bootstrap"))

    assert cfg["telemetryEndpoint"] == "https://host/telemetry/v1/traces"


def test_root_deployment_urls_unchanged():
    """Non-régression : à la racine, URLs strictement identiques à l'historique."""
    cfg = _get_config(_load_module("https://host"))

    assert cfg["telemetryEndpoint"] == "https://host/telemetry/v1/traces"
    assert cfg["keycloakTokenEndpoint"] == "https://host/auth/token"
    assert cfg["llmEndpoint"] == "https://host/llm/v1"


def test_absolute_telemetry_public_endpoint_returned_verbatim():
    """Un endpoint télémétrie absolu configuré explicitement n'est pas réécrit."""
    mod = _load_module("https://host/bootstrap")
    os.environ["DM_TELEMETRY_PUBLIC_ENDPOINT"] = "https://collector.example/v1/traces"
    try:
        mod.settings.telemetry_public_endpoint = "https://collector.example/v1/traces"
        assert mod._resolve_public_telemetry_endpoint() == "https://collector.example/v1/traces"
    finally:
        os.environ.pop("DM_TELEMETRY_PUBLIC_ENDPOINT", None)
