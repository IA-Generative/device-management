"""DM_RUNTIME_MODE=llm — deployment dédié : seul le proxy (+ sondes/métriques) sert.

Le pod llm est stateless (pas de PVC, pas de /config, pas de catalogue) : c'est
lui qui scale horizontalement derrière le LB (critère 7, volet déploiement).
"""
from fastapi.testclient import TestClient

from tests.llm_utils import BackendRecorder, load_module


def test_llm_mode_serves_only_proxy_probes_and_metrics():
    mod = load_module({"DM_RUNTIME_MODE": "llm"})
    BackendRecorder().install()
    client = TestClient(mod.app)

    # Le proxy vit (401 sans credentials = la route répond).
    assert client.get("/llm/v1/models").status_code == 401
    # Sondes + métriques disponibles pour k8s/Prometheus.
    assert client.get("/livez").status_code == 200
    assert client.get("/metrics").status_code == 200
    # Le reste du monolithe est masqué sur ce rôle.
    assert client.get("/config/config.json").status_code == 404
    assert client.get("/config/libreoffice/config.json").status_code == 404
    assert client.get("/catalog/").status_code == 404
    assert client.post("/enroll", json={}).status_code == 404


def test_api_mode_still_serves_everything():
    mod = load_module({"DM_RUNTIME_MODE": "api"})
    BackendRecorder().install()
    client = TestClient(mod.app)
    assert client.get("/llm/v1/models").status_code == 401  # proxy présent aussi en api
    assert client.get("/config/libreoffice/config.json?profile=prod").status_code == 200
