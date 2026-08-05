import importlib
import json
import os
import sys
import tempfile

from fastapi.responses import Response
from fastapi.testclient import TestClient

# Minimal config fixture for filesystem fallback
_TEST_CONFIG = {
    "configVersion": 1,
    "config": {
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
    _config_dir = tempfile.mkdtemp(prefix="dm-test-config-")
    lo_dir = os.path.join(_config_dir, "libreoffice")
    os.makedirs(lo_dir, exist_ok=True)
    for d in [_config_dir, lo_dir]:
        for name in ("config.json", "config.prod.json"):
            with open(os.path.join(d, name), "w") as f:
                json.dump(_TEST_CONFIG, f)
    return _config_dir


def _load_module():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    cfg_dir = _ensure_config_fixture()

    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_CONFIG_DIR"] = cfg_dir
    os.environ["DM_RELAY_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_ENABLED"] = "true"
    os.environ["DM_TELEMETRY_TOKEN_SIGNING_KEY"] = "unit-test-signing-key"
    os.environ["DM_TELEMETRY_REQUIRE_TOKEN"] = "true"
    os.environ["DM_TELEMETRY_PUBLIC_ENDPOINT"] = "/telemetry/v1/traces"
    os.environ["PUBLIC_BASE_URL"] = "https://example.test/bootstrap"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATABASE_ADMIN_URL", None)

    sys.modules.pop("psycopg2", None)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    return mod


def test_config_exposes_public_telemetry_settings_and_uses_token_endpoint_for_key():
    mod = _load_module()
    client = TestClient(mod.app)

    res = client.get("/config/libreoffice/config.json?profile=prod")
    assert res.status_code == 200
    body = res.json()
    cfg = body.get("config", {})

    assert cfg.get("telemetryEnabled") is True
    assert cfg.get("telemetryAuthorizationType") == "Bearer"
    # PUBLIC_BASE_URL porte un préfixe d'ingress (/bootstrap) : l'endpoint
    # télémétrie relatif est servi à la RACINE de l'origine, SANS le préfixe —
    # c'est le plugin qui re-base le path sur bootstrapUrl (préfixe compris) ;
    # le préfixer aussi côté DM doublait /bootstrap → 404 (constaté DGX).
    assert cfg.get("telemetryEndpoint") == "https://example.test/telemetry/v1/traces"
    # telemetryKey is treated as a secret and is scrubbed unless relay auth is provided.
    assert cfg.get("telemetryKey", "") == ""
    assert int(cfg.get("telemetryKeyTtlSeconds")) > 0

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    assert token_res.status_code == 200
    token = token_res.json().get("telemetryKey")
    assert isinstance(token, str) and token


def test_telemetry_relay_rejects_missing_token():
    mod = _load_module()
    client = TestClient(mod.app)

    res = client.post(
        "/telemetry/v1/traces",
        data=b'{"resourceSpans":[]}',
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 401


def test_telemetry_relay_accepts_valid_token(monkeypatch):
    mod = _load_module()
    client = TestClient(mod.app)

    def _fake_forward(body: bytes, *, content_type: str, user_agent: str | None):
        assert body == b"test-payload"
        assert content_type == "application/x-protobuf"
        return Response(content=b"ok", status_code=202, headers={"Content-Type": "text/plain"})

    monkeypatch.setattr(mod, "_forward_telemetry_to_upstream", _fake_forward)

    token_res = client.get("/telemetry/token?profile=prod&device=libreoffice")
    assert token_res.status_code == 200
    token = token_res.json().get("telemetryKey")
    assert token

    res = client.post(
        "/telemetry/v1/traces",
        data=b"test-payload",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert res.status_code == 202
    assert res.text == "ok"


# ── Attributs OTLP typés ────────────────────────────────────────────────
# Le plugin encode désormais ses attributs avec les types OTLP (intValue,
# boolValue, doubleValue). Ne lire que stringValue faisait arriver tous les
# compteurs VIDES dans device_telemetry_events — la vue « activité appareil »
# de l'admin perdait les données que Tempo, lui, recevait intactes.

def test_otlp_attribute_values_are_read_typed():
    mod = _load_module()

    assert mod._otlp_attr_value({"stringValue": "document.read"}) == "document.read"
    # OTLP/JSON sérialise int64 en chaîne : "42" doit revenir en 42.
    assert mod._otlp_attr_value({"intValue": "42"}) == 42
    assert mod._otlp_attr_value({"intValue": 7}) == 7
    assert mod._otlp_attr_value({"boolValue": True}) is True
    assert mod._otlp_attr_value({"boolValue": False}) is False
    assert mod._otlp_attr_value({"doubleValue": 0.5}) == 0.5
    assert mod._otlp_attr_value({}) == ""
    assert mod._otlp_attr_value(None) == ""


def test_persisted_spans_keep_typed_attributes(monkeypatch):
    mod = _load_module()

    captured = {}

    class _Cursor:
        def executemany(self, _sql, rows):
            captured["rows"] = rows

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    class _FakePg:
        @staticmethod
        def connect(_dsn):
            return _Conn()

    monkeypatch.setattr(mod, "psycopg2", _FakePg)
    monkeypatch.setattr(mod, "_db_url_bootstrap", lambda: "postgresql://fake")

    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "mirai-libreoffice"}},
                {"key": "service.version", "value": {"stringValue": "1.0.0"}},
            ]},
            "scopeSpans": [{"spans": [{
                "name": "AssistantRun",
                "startTimeUnixNano": 1753600000000000000,
                "attributes": [
                    {"key": "step.name", "value": {"stringValue": "document.read"}},
                    {"key": "assistant.iterations", "value": {"intValue": "3"}},
                    {"key": "assistant.ok", "value": {"boolValue": True}},
                    {"key": "assistant.duration_ms", "value": {"intValue": "1234"}},
                    {"key": "ratio", "value": {"doubleValue": 0.5}},
                ],
            }]}],
        }],
    }
    mod._persist_telemetry_spans(json.dumps(payload).encode("utf-8"), "uuid-test")

    rows = captured["rows"]
    assert len(rows) == 1
    client_uuid, _email, span_name, _ts, attributes_json, plugin_version = rows[0]
    assert client_uuid == "uuid-test"
    assert span_name == "AssistantRun"
    assert plugin_version == "1.0.0"

    attributes = json.loads(attributes_json)
    assert attributes["step.name"] == "document.read"
    assert attributes["assistant.iterations"] == 3
    assert attributes["assistant.ok"] is True
    assert attributes["assistant.duration_ms"] == 1234
    assert attributes["ratio"] == 0.5


def test_otlp_composite_and_unknown_values(caplog):
    """Les six variantes d'AnyValue sont lues. N'en couvrir qu'une partie
    reproduirait le défaut corrigé : une valeur qui disparaît sans bruit."""
    m = _load_module()

    assert m._otlp_attr_value({"arrayValue": {"values": [
        {"stringValue": "a"}, {"intValue": "2"}, {"boolValue": True}]}}) == ["a", 2, True]
    assert m._otlp_attr_value({"kvlistValue": {"values": [
        {"key": "n", "value": {"intValue": "3"}},
        {"key": "s", "value": {"stringValue": "x"}}]}}) == {"n": 3, "s": "x"}
    assert m._otlp_attr_value({"bytesValue": "aGVsbG8="}) == "aGVsbG8="
    assert m._otlp_attr_value({"arrayValue": {}}) == []

    # Un type hors spécification est journalisé, pas avalé en silence.
    with caplog.at_level("WARNING"):
        assert m._otlp_attr_value({"quantumValue": 1}) == ""
    assert any("AnyValue OTLP inconnu" in r.getMessage() for r in caplog.records)


def test_jsonb_list_tolerates_every_shape():
    """Le parse des `hypotheses` existait en deux exemplaires, dont le mien —
    le moins robuste : un JSON invalide y levait, donc un 500 sur une page
    publique. Une seule implémentation, celle qui enveloppe au lieu de lever."""
    m = _load_module()
    assert m._jsonb_list(["a", "b"]) == ["a", "b"]
    assert m._jsonb_list('["a", "b"]') == ["a", "b"]
    assert m._jsonb_list("pas du json") == ["pas du json"]
    assert m._jsonb_list(None) == []
    assert m._jsonb_list("") == []
    assert m._jsonb_list([]) == []
