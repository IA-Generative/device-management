"""Tests de durcissement de l'endpoint LLM `suggest` (app/admin/suggest_utils.py).

Reproduit les vecteurs testés par l'auditeur Advens le 23/06 et vérifie qu'ils
sont neutralisés :
  - SSRF / file:// via readme_url
  - zip-bomb / gros upload
  - parsing fragile de la sortie LLM (l'IndexError/502 historique)
  - injection indirecte dans les champs suggérés
  - rate limit
"""

import io
import json
import zipfile

import pytest
from fastapi import HTTPException

from app.admin import suggest_utils as su


# --- Anti-SSRF ---------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://evil/x",
    "ftp://host/x",
    "http://127.0.0.1/x",          # loopback
    "http://10.0.0.5/x",           # privé
    "http://192.168.1.1/x",        # privé
    "http://169.254.169.254/latest/meta-data",  # métadonnées cloud (link-local)
    "http://[::1]/x",              # loopback IPv6
])
def test_validate_public_url_blocks(url):
    with pytest.raises(HTTPException) as exc:
        su.validate_public_url(url)
    assert exc.value.status_code == 400


def test_validate_public_url_allows_public_ip():
    # IP littérale publique (pas de DNS réseau requis)
    assert su.validate_public_url("http://93.184.216.34/readme.md") == "http://93.184.216.34/readme.md"


# --- Zip-bomb / bornes -------------------------------------------------------
def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_parse_plugin_zip_nominal():
    manifest = {"slug": "demo", "name": "Demo", "description": "d"}
    z = _zip_bytes({"dm-manifest.json": json.dumps(manifest), "readme.md": "# Demo"})
    out = su.parse_plugin_zip(z)
    assert out["has_manifest"] is True
    assert out["dm_manifest"]["slug"] == "demo"


def test_parse_plugin_zip_rejects_oversized_upload(monkeypatch):
    monkeypatch.setattr(su, "MAX_UPLOAD_BYTES", 16)
    with pytest.raises(HTTPException) as exc:
        su.parse_plugin_zip(b"x" * 64)
    assert exc.value.status_code == 413


def test_parse_plugin_zip_rejects_zip_bomb(monkeypatch):
    monkeypatch.setattr(su, "MAX_DECOMPRESSED_BYTES", 100)
    z = _zip_bytes({"readme.md": "A" * 10000})  # se décompresse bien au-delà de 100o
    with pytest.raises(HTTPException) as exc:
        su.parse_plugin_zip(z)
    assert exc.value.status_code == 413


def test_parse_plugin_zip_bad_zip_returns_empty():
    out = su.parse_plugin_zip(b"not a zip at all")
    assert out["dm_manifest"] is None
    assert out["extracted"] == []


# --- Parsing LLM défensif (le crash 502/IndexError historique) ---------------
def test_extract_json_nude():
    assert su.extract_suggestion_json('{"name": "x"}') == {"name": "x"}


def test_extract_json_markdown_block():
    assert su.extract_suggestion_json('```json\n{"name": "x"}\n```') == {"name": "x"}


def test_extract_json_embedded_in_prose():
    assert su.extract_suggestion_json('Voici: {"name": "x"} fin')["name"] == "x"


@pytest.mark.parametrize("bad", [
    "",
    "pas du json",
    '```{"a":1',                  # backticks impairs (déclenchait IndexError avant)
    '{"a": "unterminated',        # 'Unterminated string' du 23/06
    '{"a": "\\x"}',               # 'Invalid \\escape' du 23/06
])
def test_extract_json_raises_valueerror_not_indexerror(bad):
    with pytest.raises(ValueError):  # JSONDecodeError est un ValueError
        su.extract_suggestion_json(bad)


# --- Anti-injection indirecte ------------------------------------------------
def test_sanitize_drops_unknown_keys():
    out = su.sanitize_suggestion({"name": "x", "__proto__": "evil", "rogue": 1})
    assert "name" in out and "__proto__" not in out and "rogue" not in out


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "data:text/html,<script>",
    "file:///etc/passwd",
])
def test_sanitize_neutralizes_dangerous_urls(bad_url):
    out = su.sanitize_suggestion({"homepage_url": bad_url, "icon_url": bad_url})
    assert out["homepage_url"] == ""
    assert out["icon_url"] == ""


def test_sanitize_keeps_safe_urls_and_inline_icon():
    out = su.sanitize_suggestion({
        "homepage_url": "https://example.org",
        "icon_url": "data:image/png;base64,AAAA",
    })
    assert out["homepage_url"] == "https://example.org"
    assert out["icon_url"].startswith("data:image/png")


# --- Rate limit --------------------------------------------------------------
def test_rate_limit_window():
    key = "suggest:test-ip"
    assert su.rate_limit_ok(key, limit=3, window_seconds=60, now=1000.0)
    assert su.rate_limit_ok(key, limit=3, window_seconds=60, now=1000.1)
    assert su.rate_limit_ok(key, limit=3, window_seconds=60, now=1000.2)
    # 4e dans la fenêtre → refusé
    assert su.rate_limit_ok(key, limit=3, window_seconds=60, now=1000.3) is False
    # après la fenêtre → de nouveau autorisé
    assert su.rate_limit_ok(key, limit=3, window_seconds=60, now=1100.0)


def test_rate_limit_disabled_when_limit_zero():
    for i in range(100):
        assert su.rate_limit_ok("k", limit=0, window_seconds=60, now=float(i))
