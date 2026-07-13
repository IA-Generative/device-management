"""FORCE_LLM_ENDPOINT_OVERRIDE — override du llmEndpoint renvoyé au plugin.

Couvre les critères 3 et 4 : défaut ON → llmEndpoint/llm_base_urls/
relayAssistantBaseUrl = URL publique du proxy et la clé backend ne sort JAMAIS ;
OFF → mode direct historique. Plus : mint du llmToken par client, non-fuite via
le cache, bascule à chaud, priorité sur les overrides catalogue.
"""
import os

from fastapi.testclient import TestClient

from tests.llm_utils import BACKEND_TOKEN, BACKEND_URL, PROXY_PUBLIC_URL, enroll, load_module

_ENRICHED = {"X-Plugin-Version": "1.0.0", "X-Client-UUID": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a"}


def _get_config(client, headers=None):
    res = client.get("/config/libreoffice/config.json?profile=prod", headers=headers or {})
    assert res.status_code == 200, res.text
    return res.json()["config"]


# ── Critère 3 : défaut ON → tout pointe vers le proxy, zéro clé backend ──────

def test_override_is_on_by_default_anonymous():
    mod = load_module()
    client = TestClient(mod.app)
    cfg = _get_config(client)
    assert cfg["llmEndpoint"] == PROXY_PUBLIC_URL
    assert cfg["llm_base_urls"] == PROXY_PUBLIC_URL
    assert cfg["relayAssistantBaseUrl"] == PROXY_PUBLIC_URL
    assert cfg["llm_api_tokens"] == ""  # la clé backend ne transite jamais
    assert cfg["llmToken"] == ""        # pas d'identité relay → pas de token


def test_override_mints_per_client_llm_token_with_relay_auth():
    mod = load_module()
    client = TestClient(mod.app)
    relay_headers = enroll(client)
    cfg = _get_config(client, {**relay_headers, **_ENRICHED})
    assert cfg["llmEndpoint"] == PROXY_PUBLIC_URL
    token = cfg["llmToken"]
    assert token and token == cfg["llm_api_tokens"]
    assert token != BACKEND_TOKEN  # jamais la clé backend
    assert isinstance(cfg["llmTokenExpiresAt"], int)
    from app.llm.tokens import verify_llm_token
    claims = verify_llm_token(token)
    assert claims["email"] == "user@example.com"
    assert claims["client_uuid"] == "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a"


def test_default_proxy_url_derives_from_public_base_url():
    mod = load_module()
    os.environ.pop("PUBLIC_LLM_PROXY_URL", None)  # hot : relu par requête
    client = TestClient(mod.app)
    cfg = _get_config(client)
    assert cfg["llmEndpoint"] == "https://example.test/bootstrap/llm/v1"


# ── Critère 4 : OFF → mode direct historique, non-régression incluse ─────────

def test_override_false_returns_direct_backend_url():
    mod = load_module({"FORCE_LLM_ENDPOINT_OVERRIDE": "false"})
    client = TestClient(mod.app)
    cfg = _get_config(client)
    assert cfg["llmEndpoint"] == BACKEND_URL
    assert cfg["llm_base_urls"] == BACKEND_URL
    assert cfg["llm_api_tokens"] == ""  # scrub sans relay auth (comportement historique)
    assert cfg["relayAssistantBaseUrl"] == "https://example.test/bootstrap/relay-assistant"


def test_override_false_keeps_legacy_secret_reveal_with_relay_key():
    mod = load_module({"FORCE_LLM_ENDPOINT_OVERRIDE": "false"})
    client = TestClient(mod.app)
    relay_headers = enroll(client)
    cfg = _get_config(client, relay_headers)
    assert cfg["llm_api_tokens"] == BACKEND_TOKEN  # non-régression mode direct
    assert cfg["llmEndpoint"] == BACKEND_URL


# ── Bascule à chaud (même mécanique que l'onglet Config admin) ───────────────

def test_hot_toggle_without_reload():
    """Flux de prod réel : PUT /admin/api/config → apply_state() sur chaque pod
    (mutation os.environ + hook d'invalidation du cache /config) → la réponse
    bascule sans redémarrage, y compris pour les requêtes anonymes cachées."""
    from app import runtime_config
    mod = load_module()
    client = TestClient(mod.app)
    runtime_config.snapshot_baseline(force=True)
    assert _get_config(client)["llmEndpoint"] == PROXY_PUBLIC_URL  # (mise en cache)
    runtime_config.apply_state({"FORCE_LLM_ENDPOINT_OVERRIDE": False})
    assert _get_config(client)["llmEndpoint"] == BACKEND_URL
    runtime_config.apply_state({"FORCE_LLM_ENDPOINT_OVERRIDE": True})
    assert _get_config(client)["llmEndpoint"] == PROXY_PUBLIC_URL
    runtime_config.apply_state({})  # reset baseline (clé non définie) → défaut ON
    assert _get_config(client)["llmEndpoint"] == PROXY_PUBLIC_URL


def test_editable_from_admin_config_registry():
    """Les clés du proxy sont pilotables depuis l'onglet Config (EDITABLE_KEYS)."""
    from app import runtime_config
    for key in ("FORCE_LLM_ENDPOINT_OVERRIDE", "PUBLIC_LLM_PROXY_URL",
                "LLM_QUOTA_REQUESTS_PER_MINUTE", "LLM_BACKENDS", "LLM_GUARDRAILS"):
        assert key in runtime_config.EDITABLE_KEYS, key
    spec = runtime_config.EDITABLE_KEYS["FORCE_LLM_ENDPOINT_OVERRIDE"]
    assert spec.type == "bool" and spec.hot_reloadable


# ── Cache : jamais de token par client dans le cache partagé ─────────────────

def test_cache_never_leaks_per_client_token():
    mod = load_module()
    client = TestClient(mod.app)
    relay_headers = enroll(client)
    # 1) Requête relay-authentifiée SANS en-têtes d'enrichissement : ne doit ni
    #    lire ni alimenter le cache (bypass sur credentials relay).
    res = client.get("/config/libreoffice/config.json?profile=prod", headers=relay_headers)
    token = res.json()["config"]["llmToken"]
    assert token
    assert res.headers["Cache-Control"] == "no-store"
    # 2) Requête anonyme identique : aucune trace du token du client précédent.
    anon = client.get("/config/libreoffice/config.json?profile=prod")
    assert token not in anon.text
    assert anon.json()["config"]["llmToken"] == ""


# ── Priorité sur les overrides catalogue (plugin_env_overrides) ──────────────

def test_override_wins_over_catalog_overrides():
    """_apply_llm_proxy_overrides s'applique APRÈS _apply_catalog_overrides :
    même si le catalogue force llm_base_urls, le proxy gagne quand FORCE=true."""
    mod = load_module()
    cfg = {"config": {
        "llm_base_urls": "https://catalog-override.test/v1",  # posé par le catalogue
        "llm_api_tokens": "catalog-token",
    }}
    out = mod._apply_llm_proxy_overrides(cfg, relay_ok=False, relay_meta=None)
    assert out["config"]["llm_base_urls"] == PROXY_PUBLIC_URL
    assert out["config"]["llmEndpoint"] == PROXY_PUBLIC_URL
    assert out["config"]["llm_api_tokens"] == ""


# ── Embedder : embdModel/embdUrl/embdToken en miroir du LLM (réutilise /llm/v1) ──
# L'embedder ne crée NI endpoint NI variable d'URL/token : embdUrl suit llmEndpoint,
# embdToken suit llmToken ; seul EMBD_MODEL_NAME est une nouvelle variable d'env.

def test_embedder_mirrors_llm_proxy_with_relay_auth():
    mod = load_module({"EMBD_MODEL_NAME": "bge-multilingual-gemma2"})
    client = TestClient(mod.app)
    relay_headers = enroll(client)
    cfg = _get_config(client, {**relay_headers, **_ENRICHED})
    assert cfg["embdModel"] == "bge-multilingual-gemma2"
    # embdUrl = proxy DM (= llmEndpoint), embdToken = token minté par client (= llmToken)
    assert cfg["embdUrl"] == cfg["llmEndpoint"] == PROXY_PUBLIC_URL
    assert cfg["embdToken"] == cfg["llmToken"]
    assert cfg["embdToken"] and cfg["embdToken"] != BACKEND_TOKEN  # jamais la clé backend


def test_embedder_anonymous_has_model_and_url_but_no_token():
    mod = load_module({"EMBD_MODEL_NAME": "bge-multilingual-gemma2"})
    client = TestClient(mod.app)
    cfg = _get_config(client)  # anonyme : pas d'identité relay
    assert cfg["embdModel"] == "bge-multilingual-gemma2"
    assert cfg["embdUrl"] == PROXY_PUBLIC_URL
    assert cfg["embdToken"] == ""  # comme llmToken : pas d'identité → pas de token


def test_embedder_direct_mode_when_override_off():
    mod = load_module({"FORCE_LLM_ENDPOINT_OVERRIDE": "false",
                       "EMBD_MODEL_NAME": "embedding"})  # alias dgx/prod-sdid
    client = TestClient(mod.app)
    cfg = _get_config(client)
    assert cfg["embdModel"] == "embedding"
    assert cfg["embdUrl"] == BACKEND_URL == cfg["llmEndpoint"]  # direct, hérite du LLM


def test_embedder_model_empty_when_unset():
    mod = load_module()  # pas d'EMBD_MODEL_NAME → embedder désactivé (modèle vide)
    client = TestClient(mod.app)
    cfg = _get_config(client)
    assert cfg.get("embdModel", "") == ""
    assert cfg["embdUrl"] == cfg["llmEndpoint"]  # URL cohérente même sans modèle


def test_embedder_inherits_base_path_prefix():
    """Non-régression DGX : embdUrl hérite du préfixe /bootstrap via llmEndpoint."""
    mod = load_module({"EMBD_MODEL_NAME": "embedding"})
    os.environ.pop("PUBLIC_LLM_PROXY_URL", None)  # dériver de PUBLIC_BASE_URL (préfixé)
    client = TestClient(mod.app)
    cfg = _get_config(client)
    assert cfg["embdUrl"] == "https://example.test/bootstrap/llm/v1"


def test_embd_model_name_is_editable_key():
    from app import runtime_config
    spec = runtime_config.EDITABLE_KEYS.get("EMBD_MODEL_NAME")
    assert spec is not None and spec.type == "str" and spec.hot_reloadable
