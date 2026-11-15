"""Intégration (vrai Postgres) — clés d'API personnelles, de la création à l'appel.

Marqué `integration`. Même montage que tests/test_deploy_guidance_pg.py.
"""
# ruff: noqa: F811 — les fixtures importées sont redéclarées en paramètres de test
from __future__ import annotations

import json

import pytest

from app.services import api_keys
from tests.package_trust_helpers import xpi
from tests.test_deploy_guidance_pg import (  # noqa: F401 — fixtures pytest
    _DEVICE_TYPE,
    _SLUG,
    _TOKEN,
    _db_url,
    _page,
    mod,
    site,
)

pytestmark = pytest.mark.integration

OWNER = "titulaire@example.test"


@pytest.fixture
def keys(site):
    """Fabrique de clés ; elles sont retirées en sortie."""
    import psycopg2

    def make(**overrides):
        fields = {"label": "it", "owner_email": OWNER, "scopes": ["write"], "plugins": [], "ttl_days": 30}
        fields.update(overrides)
        conn = psycopg2.connect(site.db_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                return api_keys.create(cur, **fields)
        finally:
            conn.close()

    yield make
    site.sql("DELETE FROM api_keys WHERE owner_email IN (%s, %s)", (OWNER, "autre@example.test"))


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _deploy(site, headers, version, slug=_SLUG):
    return site.http.post(f"/api/plugins/{slug}/deploy", headers=headers,
                          data={"strategy": "immediate", "version": version},
                          files={"binary": (f"{slug}-{version}.xpi", xpi(version), "application/octet-stream")})


def test_e2e_a_key_calls_the_api_and_the_audit_names_its_owner(site, keys):
    token, row = keys(label="CI du plugin")
    res = _deploy(site, _bearer(token), "1.0.0")
    assert res.status_code == 201, res.text
    audit = site.sql("SELECT actor_email, actor_sub, payload FROM admin_audit_log "
                     "WHERE action = 'package.import' ORDER BY id DESC LIMIT 1")[0]
    assert audit[0] == OWNER and audit[1] == f"apikey:{row['key_id']}"
    assert site.sql("SELECT created_by FROM campaigns WHERE plugin_id = %s ORDER BY id DESC LIMIT 1",
                    (site.plugin_id,))[0][0] == OWNER
    # Même clé par l'en-tête du jeton de service, et sur une route d'administration ouverte au jeton.
    assert site.http.get("/api/campaigns", headers={"X-Admin-Token": token}).status_code == 200
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/versions/upload", headers=_bearer(token),
                         data={"version": "1.1.0", "action": "publish_only", "environment": "it"},
                         files={"binary": (f"{_SLUG}-1.1.0.xpi", xpi("1.1.0"), "application/octet-stream")},
                         follow_redirects=False)
    assert res.status_code == 303, res.text
    upload = site.sql("SELECT actor_email FROM admin_audit_log WHERE action = 'version.upload' ORDER BY id DESC LIMIT 1")
    assert upload[0][0] == OWNER
    used = site.sql("SELECT last_used_at, last_used_ip FROM api_keys WHERE id = %s", (row["id"],))[0]
    assert used[0] is not None and used[1]


def test_e2e_the_secret_is_never_stored_listed_nor_logged(site, keys):
    token, row = keys()
    secret = token.rsplit("_", 1)[1]
    stored = site.sql("SELECT row_to_json(k)::text FROM api_keys k WHERE id = %s", (row["id"],))[0][0]
    assert secret not in stored and token not in stored
    assert _deploy(site, _bearer(token), "1.0.0").status_code == 201
    audit = site.sql("SELECT string_agg(payload::text, ' ') FROM admin_audit_log")[0][0] or ""
    assert secret not in audit
    page = _page(site, "/admin/api-keys")
    assert secret not in page and f"dmk_{row['key_id']}_" in page and OWNER in page


def test_e2e_scopes_plugins_expiry_and_revocation_are_enforced(site, keys):
    reader, _ = keys(scopes=["read"])
    assert site.http.get("/api/campaigns", headers=_bearer(reader)).status_code == 200
    res = _deploy(site, _bearer(reader), "1.0.0")
    assert res.status_code == 403 and res.json()["code"] == "insufficient_scope"

    bounded, _ = keys(plugins=["un-autre-plugin"])
    res = _deploy(site, _bearer(bounded), "1.0.0")
    assert res.status_code == 403 and res.json()["code"] == "plugin_not_allowed"
    # Une clé bornée ne peut pas écrire sur une route qui ne nomme aucun plugin.
    res = site.http.post("/api/artifacts", headers=_bearer(bounded), data={"device_type": _DEVICE_TYPE, "version": "1.0.0"},
                         files={"binary": ("p-1.0.0.xpi", xpi("1.0.0"), "application/octet-stream")})
    assert res.status_code == 403
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/versions/upload", headers=_bearer(bounded),
                         data={"version": "1.0.0", "action": "publish_only", "environment": "it"},
                         files={"binary": (f"{_SLUG}-1.0.0.xpi", xpi("1.0.0"), "application/octet-stream")},
                         follow_redirects=False)
    assert res.status_code == 403 and res.json()["code"] == "plugin_not_allowed"
    allowed, _ = keys(plugins=[_SLUG])
    assert _deploy(site, _bearer(allowed), "1.0.0").status_code == 201

    expired, row = keys()
    site.sql("UPDATE api_keys SET expires_at = now() - interval '1 minute' WHERE id = %s", (row["id"],))
    res = site.http.get("/api/campaigns", headers=_bearer(expired))
    assert res.status_code == 401 and res.json()["code"] == "expired_key"

    forged = allowed[:-4] + ("AAAA" if not allowed.endswith("AAAA") else "BBBB")
    res = site.http.get("/api/campaigns", headers=_bearer(forged))
    assert res.status_code == 401 and res.json()["code"] == "invalid_key"
    assert site.versions() == {"1.0.0": "published"}, "aucun des appels refusés n'a rien écrit"


def test_e2e_admin_page_creates_shows_once_and_revokes(site, keys):
    _page(site, "/admin/campaigns")     # session d'administration
    res = site.http.post("/admin/api-keys", headers={"Accept": "application/json"},
                         data={"label": "Poste de Camille", "owner_email": "autre@example.test",
                               "ttl_days": "30", "scope_read": "1", "plugins": _SLUG})
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    token = res.json()["token"]
    assert token.startswith("dmk_") and res.json()["label"] == "Poste de Camille"
    created = site.sql("SELECT actor_email, payload FROM admin_audit_log WHERE action = 'api_key.create' "
                       "ORDER BY id DESC LIMIT 1")[0]
    assert created[1]["owner_email"] == "autre@example.test" and token not in json.dumps(created[1])
    assert site.http.get("/api/campaigns", headers=_bearer(token)).status_code == 200

    # Saisie refusée : message lisible, rien de créé.
    before = site.sql("SELECT count(*) FROM api_keys")[0][0]
    for bad in ({"label": "", "owner_email": OWNER, "scope_read": "1"},
                {"label": "x", "owner_email": OWNER},                                   # aucune portée
                {"label": "x", "owner_email": OWNER, "scope_read": "1", "ttl_days": "5000"}):
        res = site.http.post("/admin/api-keys", headers={"Accept": "application/json"}, data=bad)
        assert res.status_code == 400 and res.json()["error"], bad
    assert site.sql("SELECT count(*) FROM api_keys")[0][0] == before

    # La page ne remontre jamais la clé, et la révocation coupe l'accès aussitôt.
    page = _page(site, "/admin/api-keys")
    assert "Poste de Camille" in page and token not in page
    key_pk = site.sql("SELECT id FROM api_keys WHERE label = 'Poste de Camille'")[0][0]
    res = site.http.post(f"/admin/api-keys/{key_pk}/revoke", follow_redirects=False)
    assert res.status_code == 303
    res = site.http.get("/api/campaigns", headers=_bearer(token))
    assert res.status_code == 401 and res.json()["code"] == "revoked_key"
    assert site.sql("SELECT count(*) FROM admin_audit_log WHERE action = 'api_key.revoke'")[0][0] >= 1


def test_e2e_a_key_cannot_manage_keys_and_the_service_token_still_works(site, keys):
    token, _ = keys()
    site.http.cookies.clear()
    for method, url in (("get", "/admin/api-keys"), ("post", "/admin/api-keys")):
        res = getattr(site.http, method)(url, headers=_bearer(token), follow_redirects=False)
        assert res.status_code != 200, "une clé d'API ne crée ni ne liste de clés"
    assert site.http.get("/api/campaigns", headers={"X-Admin-Token": _TOKEN}).status_code == 200
    assert site.http.get("/api/campaigns").status_code == 401
