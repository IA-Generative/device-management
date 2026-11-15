"""Intégration (vrai Postgres) — tirer les releases d'un plugin depuis GitHub (issue #108).

Trois volets :
  - la migration 011 et db/schema.sql décrivent les mêmes tables, et la migration
    se rejoue sans effet ;
  - le relevé du worker : états des releases, échec réseau noté sur le plugin,
    essais espacés, une ligne de journal par changement d'état ;
  - de bout en bout par les routes HTTP, contre un faux GitHub réel
    (scripts/faux_github.py) : source déclarée → « Vérifier maintenant » → relevé →
    « Importer » une release signée → aperçu → envoi par `catalog_version_upload`
    avec l'`upload_id` → artefact dont la provenance nomme la release.

Marqué `integration`. Même montage que tests/test_deploy_guidance_pg.py.

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_github_releases_pg.py -m integration`
"""
# ruff: noqa: F811 — les fixtures importées sont redéclarées en paramètres de test
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import logging
import os
import threading
import time
import zipfile

import httpx
import pytest

from tests.test_deploy_guidance_pg import (  # noqa: F401 — fixtures pytest
    _DEVICE_TYPE,
    _SLUG,
    _TOKEN,
    _db_url,
    mod,
    site,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "ia-test/plugin-it"
COMPONENT = "plugin-it"
GH_TOKEN = "ghs_jetonIntegration0123456789abcdef"
SOURCE = {"repository": REPO, "component": COMPONENT, "asset_pattern": f"{_SLUG}-*.xpi", "channel": "general"}


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_REPO_ROOT, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gh():
    from app.services import github_releases
    return github_releases


def _xpi(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("manifest.json", (2026, 1, 1, 0, 0, 0)), json.dumps({
            "name": "it", "version": version,
            "browser_specific_settings": {"gecko": {"id": "it-108@example.invalid"}}}))
    return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════

def _columns(cur, schema: str) -> dict:
    cur.execute("""
        SELECT table_name, column_name, data_type, is_nullable, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = %s AND (table_name IN ('plugin_github_releases', 'plugin_github_state')
              OR (table_name = 'plugins' AND column_name = 'github_source'))
    """, (schema,))
    return {(t, c): rest for t, c, *rest in cur.fetchall()}


def test_migration_011_matches_schema_sql_and_replays(_db_url):
    """Une base montée par les migrations et une base montée par db/schema.sql
    doivent porter les mêmes colonnes ; rejouer la migration ne change rien."""
    import psycopg2
    migration = _load("alembic/versions/012_github_releases.py", "migration_012")
    assert migration.revision == "012" and migration.down_revision == "011"

    conn = psycopg2.connect(_db_url)
    try:
        with conn.cursor() as cur:
            expected = _columns(cur, "public")
            assert ("plugins", "github_source") in expected, "db/schema.sql porte la colonne"
            assert len(expected) == 1 + 15 + 9

            class _Op:
                @staticmethod
                def execute(sql):
                    cur.execute(sql)

            migration.op = _Op
            cur.execute("CREATE SCHEMA it_migration_011")
            cur.execute("SET LOCAL search_path TO it_migration_011")
            cur.execute("CREATE TABLE plugins (id SERIAL PRIMARY KEY)")
            migration.upgrade()
            migration.upgrade()
            assert _columns(cur, "it_migration_011") == expected

            migration.downgrade()
            assert _columns(cur, "it_migration_011") == {}
            migration.upgrade()
            assert _columns(cur, "it_migration_011") == expected
    finally:
        conn.rollback()
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Relevé (le travail du worker), GitHub simulé par un transport factice
# ═════════════════════════════════════════════════════════════════════════════

def _api_release(version: str, *, component: str = COMPONENT, signed: bool = True, assets=None) -> dict:
    tag = f"{component}-v{version}"
    name = f"{_SLUG}-{version}.xpi"
    base = "https://api.github.test/repos/x/y/releases/assets"
    if assets is None:
        assets = [{"name": name, "url": f"{base}/1"}] + ([{"name": name + ".sig.json", "url": f"{base}/2"}] if signed else [])
    return {"tag_name": tag, "prerelease": "-" in version, "draft": False, "body": f"Notes {version}",
            "published_at": "2026-11-01T10:00:00Z", "html_url": f"https://github.test/{tag}", "assets": assets}


class _FakeGithub:
    """Transport httpx : répond ce qu'on lui dit, compte les appels."""

    def __init__(self, releases):
        self.releases = releases
        self.failure: httpx.Response | Exception | None = None
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert request.headers.get("Authorization") == f"Bearer {GH_TOKEN}"
        if isinstance(self.failure, Exception):
            raise self.failure
        if self.failure is not None:
            return self.failure
        return httpx.Response(200, json=self.releases)

    def client(self) -> httpx.Client:
        return _gh()._client(transport=httpx.MockTransport(self))


@pytest.fixture
def sourced(site, monkeypatch):
    """Le plugin de test avec une source GitHub déclarée, et l'environnement du relevé."""
    monkeypatch.setenv("DM_GITHUB_API_URL", "https://api.github.test")
    monkeypatch.setenv("DM_GITHUB_TOKEN", GH_TOKEN)
    monkeypatch.setenv("DM_GITHUB_POLL_INTERVAL", "900")
    monkeypatch.delenv("DM_PACKAGE_SIGNATURE_POLICY", raising=False)
    site.sql("UPDATE plugins SET github_source = %s WHERE id = %s", (json.dumps(SOURCE), site.plugin_id))
    return site


def _rows(site) -> dict:
    return {version: (state, reason) for version, state, reason in site.sql(
        "SELECT version, state, reason FROM plugin_github_releases WHERE plugin_id = %s", (site.plugin_id,))}


def _state(site) -> dict:
    [row] = site.sql("""SELECT status, error_code, message, failures, requested_at,
                               EXTRACT(EPOCH FROM (next_attempt_at - checked_at))::int
                        FROM plugin_github_state WHERE plugin_id = %s""", (site.plugin_id,))
    return dict(zip(("status", "error_code", "message", "failures", "requested_at", "wait"), row, strict=True))


def test_cycle_records_the_releases_and_their_states(sourced):
    gh = _gh()
    sourced.upload("1.0.0", action="publish_only")
    fake = _FakeGithub([
        _api_release("1.0.0"), _api_release("1.1.0"), _api_release("1.2.0-rc.1"),
        _api_release("1.0.5", assets=[]), _api_release("9.9.9", component="autre"),
    ])
    with fake.client() as client:
        summary = gh.run_cycle(client=client)
    assert summary["plugins"][sourced.plugin_id] == {"statut": "ok", "releases": 4}

    rows = _rows(sourced)
    assert set(rows) == {"1.0.0", "1.1.0", "1.2.0-rc.1", "1.0.5"}, "l'autre composant est écarté"
    assert rows["1.0.0"] == ("imported", None)
    assert rows["1.1.0"] == ("available", None)
    assert rows["1.2.0-rc.1"][0] == "ignored" and "Hors du canal" in rows["1.2.0-rc.1"][1]
    assert rows["1.0.5"][0] == "ignored" and "Aucun fichier" in rows["1.0.5"][1]
    state = _state(sourced)
    assert (state["status"], state["failures"], state["wait"]) == ("ok", 0, 900)

    # Pas avant l'échéance : le réveil suivant n'appelle pas GitHub.
    with fake.client() as client:
        assert gh.run_cycle(client=client)["plugins"] == {}
    assert fake.calls == 1

    # Une release retirée du dépôt quitte le relevé ; « Vérifier maintenant » n'attend pas l'échéance.
    fake.releases = [_api_release("1.1.0")]
    assert gh.request_check(sourced.plugin_id)["statut"] == "demande_transmise"
    with fake.client() as client:
        gh.run_cycle(client=client)
    assert fake.calls == 2 and set(_rows(sourced)) == {"1.1.0"}
    assert _state(sourced)["requested_at"] is None, "demande relevée"


def test_one_repository_is_fetched_once_for_all_its_plugins(sourced):
    gh = _gh()
    other = _SLUG + "-bis"
    sourced.sql("DELETE FROM plugins WHERE slug = %s", (other,))
    sourced.sql("INSERT INTO plugins (slug, name, device_type, status, github_source) VALUES (%s, 'bis', %s, 'active', %s)",
                (other, _DEVICE_TYPE, json.dumps({**SOURCE, "component": "autre"})))
    try:
        fake = _FakeGithub([_api_release("1.1.0"), _api_release("9.9.9", component="autre")])
        with fake.client() as client:
            summary = gh.run_cycle(client=client)
        assert len(summary["plugins"]) == 2 and fake.calls == 1
    finally:
        sourced.sql("DELETE FROM plugins WHERE slug = %s", (other,))


def test_network_failure_is_kept_on_the_plugin_spaced_out_and_logged_once(sourced, caplog):
    gh = _gh()
    fake = _FakeGithub([_api_release("1.1.0")])
    with fake.client() as client:
        gh.run_cycle(client=client)
    assert _rows(sourced)["1.1.0"][0] == "available"

    fake.failure = httpx.ProxyError("403 depuis http://user:motdepasse@proxy:3128")
    waits = []
    with caplog.at_level(logging.INFO, logger="dm-github-releases"):
        for expected_failures in (1, 2, 3):
            gh.request_check(sourced.plugin_id)
            with fake.client() as client:
                result = gh.run_cycle(client=client)["plugins"][sourced.plugin_id]
            assert result["statut"] == "erreur" and result["code"] == "proxy"
            state = _state(sourced)
            assert (state["status"], state["error_code"], state["failures"]) == ("error", "proxy", expected_failures)
            waits.append(state["wait"])
        assert waits == [1800, 3600, 7200], "les essais s'espacent"

        # Sans demande, l'essai suivant attend son tour : aucun appel.
        calls = fake.calls
        with fake.client() as client:
            assert gh.run_cycle(client=client)["plugins"] == {}
        assert fake.calls == calls

        failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(failures) == 1, "une ligne pour trois essais ratés"
        assert "proxy de sortie" in _state(sourced)["message"]
        assert _rows(sourced)["1.1.0"][0] == "available", "le dernier relevé réussi reste affiché"

        # Autre cause : une nouvelle ligne. Puis le rétablissement se dit, une fois.
        fake.failure = httpx.Response(403, json={"message": "non"})
        gh.request_check(sourced.plugin_id)
        with fake.client() as client:
            gh.run_cycle(client=client)
        assert _state(sourced)["error_code"] == "forbidden"
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2

        fake.failure = None
        gh.request_check(sourced.plugin_id)
        with fake.client() as client:
            gh.run_cycle(client=client)
        state = _state(sourced)
        assert (state["status"], state["error_code"], state["message"], state["failures"]) == ("ok", None, None, 0)
        assert any("rétabli" in r.getMessage() for r in caplog.records)

    everything = caplog.text + json.dumps(_state(sourced), default=str)
    assert GH_TOKEN not in everything and "motdepasse" not in everything


def test_periodic_reading_can_be_disabled_but_a_request_is_still_served(sourced, monkeypatch):
    gh = _gh()
    monkeypatch.setenv("DM_GITHUB_POLL_INTERVAL", "0")
    fake = _FakeGithub([_api_release("1.1.0")])
    with fake.client() as client:
        assert gh.run_cycle(client=client)["plugins"] == {}
    assert fake.calls == 0
    gh.request_check(sourced.plugin_id)
    with fake.client() as client:
        gh.run_cycle(client=client)
    assert fake.calls == 1 and "1.1.0" in _rows(sourced)


def test_a_noted_refusal_survives_the_next_reading(sourced):
    gh = _gh()
    fake = _FakeGithub([_api_release("1.1.0")])
    with fake.client() as client:
        gh.run_cycle(client=client)
    sourced.sql("UPDATE plugin_github_releases SET state = 'refused', reason = 'signature invalide' WHERE plugin_id = %s",
                (sourced.plugin_id,))
    gh.request_check(sourced.plugin_id)
    with fake.client() as client:
        gh.run_cycle(client=client)
    assert _rows(sourced)["1.1.0"] == ("refused", "signature invalide")


# ═════════════════════════════════════════════════════════════════════════════
# De bout en bout — routes HTTP et faux GitHub réel
# ═════════════════════════════════════════════════════════════════════════════

class _Bench:
    """Un faux GitHub sur un port local, alimenté par un dossier, et une clé d'éditeur."""

    def __init__(self, root, monkeypatch, *, token: str = GH_TOKEN):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        self.root = root
        self.key = ec.generate_private_key(ec.SECP256R1())
        public = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.fake = _load("scripts/faux_github.py", "faux_github")
        self.server = self.fake.serve(str(root), port=0, token=token)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        monkeypatch.setenv("DM_GITHUB_API_URL", self.url)
        monkeypatch.setenv("DM_GITHUB_TOKEN", GH_TOKEN)
        monkeypatch.setenv("DM_GITHUB_POLL_INTERVAL", "900")
        monkeypatch.setenv("DM_PACKAGE_PUBLISHER_KEYS", public)
        monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    def release(self, version: str, *, signed: bool = True, body: str = "", component: str = COMPONENT,
                package: bytes | None = None) -> bytes:
        from app.services import package_signing
        tag_dir = self.root / REPO / f"{component}-v{version}"
        tag_dir.mkdir(parents=True)
        package = package if package is not None else _xpi(version)
        name = f"{_SLUG}-{version}.xpi"
        (tag_dir / name).write_bytes(package)
        if signed:
            envelope = package_signing.make_envelope({
                "schema": package_signing.STATEMENT_SCHEMA, "kind": "build", "slug": _SLUG,
                "version": version, "sha256": hashlib.sha256(package).hexdigest(),
                "source": {"repository": REPO, "ref": f"refs/tags/{component}-v{version}", "commit": "a" * 40},
            }, self.key)
            (tag_dir / (name + ".sig.json")).write_text(json.dumps(envelope))
        (tag_dir / "release.json").write_text(json.dumps({"body": body, "published_at": "2026-11-02T09:30:00Z"}))
        return package

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bench(tmp_path, monkeypatch):
    b = _Bench(tmp_path / "releases", monkeypatch)
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def admin(site):
    """Le site de test avec une session d'administration (les routes GitHub n'acceptent pas le jeton de service)."""
    from app.admin.auth import SESSION_COOKIE, _sign_session
    site.http.cookies.set(SESSION_COOKIE, _sign_session({
        "sub": "it-108", "email": "admin@test.invalid", "name": "IT 108", "exp": int(time.time()) + 600}))
    return site


def _post_source(admin, **over):
    form = {**SOURCE, **over}
    return admin.http.post(f"/admin/catalog/{admin.plugin_id}/github-source", data=form, follow_redirects=False)


def _import(admin, tag: str):
    return admin.http.post(f"/admin/catalog/{admin.plugin_id}/github/import", json={"tag": tag})


def _page(admin) -> str:
    res = admin.http.get(f"/admin/catalog/{admin.plugin_id}?tab=versions")
    assert res.status_code == 200, res.text[:400]
    return res.text


def _audit(admin) -> str:
    return json.dumps(admin.sql(
        "SELECT action, payload FROM admin_audit_log WHERE actor_sub IN ('it-108', 'service') ORDER BY id DESC LIMIT 30"),
        default=str)


def test_e2e_signed_release_is_listed_imported_previewed_and_published_with_its_provenance(admin, bench):
    gh = _gh()
    package = bench.release("1.4.0", body="- Premier tirage depuis GitHub")
    bench.release("1.5.0-rc.1")
    bench.release("9.0.0", component="autre-plugin")
    tag = f"{COMPONENT}-v1.4.0"

    # 1. La source se déclare sur la fiche ; une vérification est aussitôt demandée.
    saved = _post_source(admin)
    assert saved.status_code == 303 and "github=saved" in saved.headers["location"]
    assert admin.sql("SELECT github_source FROM plugins WHERE id = %s", (admin.plugin_id,))[0][0] == SOURCE
    assert _state(admin)["requested_at"] is not None
    page = _page(admin)
    assert 'id="github-pending"' in page and "Verifier maintenant" in page

    # 2. « Vérifier maintenant » ne fait que déposer la demande ; c'est le worker qui relève.
    asked = admin.http.post(f"/admin/catalog/{admin.plugin_id}/github/check", follow_redirects=False)
    assert asked.status_code == 303 and "github=asked" in asked.headers["location"]
    assert _rows(admin) == {}, "aucun appel à GitHub depuis le processus web"
    assert gh.run_cycle()["plugins"][admin.plugin_id] == {"statut": "ok", "releases": 2}
    rows = _rows(admin)
    assert rows["1.4.0"] == ("available", None)
    assert rows["1.5.0-rc.1"][0] == "ignored"

    page = _page(admin)
    assert "Versions disponibles sur GitHub" in page and "1 nouvelle(s)" in page
    assert f'data-tag="{tag}"' in page and ">Importer</button>" in page
    assert 'dm-nature dm-nature--rc">Candidate' in page and "Hors du canal suivi" in page
    assert "fourni a cette instance" in page and GH_TOKEN not in page

    # 3. « Importer » : le DM télécharge le paquet et sa signature, et les dépose.
    imported = _import(admin, tag)
    assert imported.status_code == 200, imported.text
    payload = imported.json()
    assert payload["filename"] == f"{_SLUG}-1.4.0.xpi" and payload["version"] == "1.4.0"
    assert payload["release_notes"] == "- Premier tirage depuis GitHub" and payload["refusal"] is None
    envelope = json.loads(payload["signature"])
    assert envelope["schema"] == "dm-package-signature/1"
    assert GH_TOKEN not in imported.text
    assert admin.versions() == {}, "rien n'est publié par l'import"

    # 4. L'aperçu de l'assistant, sur le paquet déposé : nature, origine vérifiée.
    preview = admin.http.post("/admin/api/deploy/extract-version", data={
        "upload_id": payload["upload_id"], "plugin_id": str(admin.plugin_id)},
        files={"signature": ("x.sig.json", payload["signature"].encode(), "application/json")})
    assert preview.status_code == 200, preview.text
    seen = preview.json()
    assert seen["valid"] and seen["version"] == "1.4.0" and seen["extension"] == "xpi"
    assert seen["package"]["nature"] == "general" and seen["package"]["refusal"] is None
    assert seen["package"]["build"]["status"] == "verified"
    assert seen["package"]["build"]["repository"] == REPO
    # L'aperçu ne consomme pas le dépôt : on peut le rejouer.
    assert admin.http.post("/admin/api/deploy/extract-version", data={
        "upload_id": payload["upload_id"], "plugin_id": str(admin.plugin_id)}).status_code == 200

    # 5. L'envoi passe par la route de l'envoi manuel, avec l'upload_id. Sans la signature : refusé
    #    (la politique s'applique), et le dépôt survit au refus.
    upload = {"upload_id": payload["upload_id"], "version": "1.4.0", "action": "publish_only",
              "release_notes": payload["release_notes"]}
    unsigned = admin.http.post(f"/admin/catalog/{admin.plugin_id}/versions/upload", json=upload,
                               follow_redirects=False)
    assert unsigned.status_code == 422 and unsigned.json()["detail"]["code"] == "signature_required"
    assert _rows(admin)["1.4.0"][0] == "refused", "le refus se voit sur le relevé"
    assert 'dm-badge--error">Refusee' in _page(admin) and ">Reessayer</button>" in _page(admin)

    published = admin.http.post(f"/admin/catalog/{admin.plugin_id}/versions/upload",
                                json={**upload, "signature": payload["signature"]}, follow_redirects=False)
    assert published.status_code == 303, published.text
    assert admin.versions() == {"1.4.0": "published"}
    assert admin.active_campaigns() == [], "aucun déploiement automatique"

    # 6. L'artefact garde d'où il vient ; les octets importés sont ceux de la release.
    [(provenance, checksum)] = admin.sql(
        "SELECT provenance, checksum FROM artifacts WHERE device_type = %s AND version = '1.4.0'", (_DEVICE_TYPE,))
    assert provenance["github"] == {
        "repository": REPO, "tag": tag, "asset": f"{_SLUG}-1.4.0.xpi",
        "release_url": f"{bench.url}/{REPO}/releases/tag/{tag}",
        "published_at": "2026-11-02T09:30:00+00:00"}
    assert provenance["upstream"]["sha256"] == hashlib.sha256(package).hexdigest()
    assert provenance["build"]["status"] == "verified"
    [(bootstrap_info,)] = admin.sql(
        "SELECT bootstrap_info FROM artifacts WHERE device_type = %s AND version = '1.4.0'", (_DEVICE_TYPE,))
    assert "github" not in (bootstrap_info or {})
    served = admin.http.get(f"/catalog/{_SLUG}/download/{_SLUG}-1.4.0.xpi")
    assert served.status_code == 200 and "sha256:" + hashlib.sha256(served.content).hexdigest() == checksum

    # 7. Le relevé et la fiche le disent ; le dépôt est retiré ; rien de secret n'a fui.
    assert _rows(admin)["1.4.0"] == ("imported", None)
    page = _page(admin)
    assert "Tiree de GitHub" in page and 'dm-badge--success">Importee' in page
    assert "rien de nouveau" in page
    again = admin.http.post(f"/admin/catalog/{admin.plugin_id}/versions/upload",
                            json={**upload, "signature": payload["signature"]}, follow_redirects=False)
    assert again.status_code == 400, "dépôt consommé par l'import réussi"
    assert _import(admin, tag).status_code == 409, "déjà au catalogue"
    audit = _audit(admin)
    assert "plugin.github_source" in audit and "plugin.github_check" in audit and "plugin.github_import" in audit
    assert tag in audit and GH_TOKEN not in audit and GH_TOKEN not in page

    # 8. Le relevé suivant confirme l'état à partir du catalogue.
    gh.request_check(admin.plugin_id)
    gh.run_cycle()
    assert _rows(admin)["1.4.0"] == ("imported", None)


def test_e2e_same_release_imported_twice_gives_the_same_bytes(admin, bench):
    """Le dépôt porte les octets publiés, sans réécriture : même empreinte à chaque tirage."""
    gh = _gh()
    package = bench.release("2.0.0")
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    from app.admin import router
    digests = set()
    for _ in range(2):
        payload = _import(admin, f"{COMPONENT}-v2.0.0").json()
        data, filename = router._read_upload(payload["upload_id"], consume=False)
        assert filename == f"{_SLUG}-2.0.0.xpi"
        digests.add(hashlib.sha256(data).hexdigest())
    assert digests == {hashlib.sha256(package).hexdigest()}


def test_e2e_unsigned_release_is_refused_where_the_instance_requires_a_signature(admin, bench):
    gh = _gh()
    bench.release("3.0.0", signed=False)
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    state, reason = _rows(admin)["3.0.0"]
    assert state == "available" and "l'import sera refusé" in reason

    payload = _import(admin, f"{COMPONENT}-v3.0.0").json()
    assert payload["signature"] is None
    assert payload["refusal"]["code"] == "signature_required" and payload["refusal"]["confirmable"] is False
    state, reason = _rows(admin)["3.0.0"]
    assert state == "refused" and ".sig.json" in reason
    page = _page(admin)
    assert 'dm-badge--error">Refusee' in page and ">Reessayer</button>" in page
    assert admin.versions() == {}


def test_e2e_release_whose_package_announces_another_version_is_refused(admin, bench, monkeypatch):
    gh = _gh()
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "off")
    bench.release("4.0.0", package=_xpi("4.0.1"), signed=False)
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    payload = _import(admin, f"{COMPONENT}-v4.0.0").json()
    assert payload["refusal"]["code"] == "version_mismatch"
    assert _rows(admin)["4.0.0"][0] == "refused"


def test_e2e_dm_without_access_says_so_and_manual_upload_remains(admin, bench, monkeypatch):
    gh = _gh()
    bench.release("5.0.0")
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    assert _rows(admin)["5.0.0"][0] == "available"

    # GitHub devient injoignable : port fermé.
    bench.close()
    gh.request_check(admin.plugin_id)
    result = gh.run_cycle()["plugins"][admin.plugin_id]
    assert result["statut"] == "erreur" and result["code"] in ("network", "timeout")
    page = _page(admin)
    assert 'id="github-error"' in page
    assert "Ce DM n'a pas d'acces a GitHub : l'envoi manuel reste la voie." in page.replace("&#39;", "'")
    assert "1 echec(s) de suite" in page and "releve en echec" not in page, "une nouveauté reste annoncée"

    failed = _import(admin, f"{COMPONENT}-v5.0.0")
    assert failed.status_code == 502 and failed.json()["detail"]["code"] in ("network", "timeout")
    assert GH_TOKEN not in failed.text

    # L'envoi manuel fonctionne comme avant (la politique « require » exige sa signature : on la lève ici).
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "off")
    assert admin.upload("5.0.1", action="publish_only").status_code == 303


def test_e2e_bad_token_is_reported_without_echoing_it(admin, bench, monkeypatch):
    gh = _gh()
    bench.release("6.0.0")
    monkeypatch.setenv("DM_GITHUB_TOKEN", "ghs_mauvaisJeton000000000000")
    assert _post_source(admin).status_code == 303
    result = gh.run_cycle()["plugins"][admin.plugin_id]
    assert result["code"] == "unauthorized"
    page = _page(admin)
    assert "DM_GITHUB_TOKEN" in page and "ghs_mauvaisJeton" not in page
    assert "Le releve des releases a echoue" in page


def test_e2e_source_form_validates_resets_and_can_be_removed(admin, bench):
    gh = _gh()
    bad = _post_source(admin, repository="pas-un-depot")
    assert bad.status_code == 400 and "organisation/depot" in bad.json()["detail"]
    assert admin.sql("SELECT github_source FROM plugins WHERE id = %s", (admin.plugin_id,))[0][0] is None
    assert "aucune source declaree" in _page(admin)

    bench.release("7.0.0")
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    assert "7.0.0" in _rows(admin)

    # Autre canal : le relevé repart de zéro, avec une nouvelle demande.
    assert _post_source(admin, channel="all").status_code == 303
    assert _rows(admin) == {} and _state(admin)["requested_at"] is not None

    # Dépôt vide : la source est retirée, le relevé aussi.
    assert _post_source(admin, repository="").status_code == 303
    assert admin.sql("SELECT github_source FROM plugins WHERE id = %s", (admin.plugin_id,))[0][0] is None
    assert admin.sql("SELECT count(*) FROM plugin_github_state WHERE plugin_id = %s", (admin.plugin_id,))[0][0] == 0
    assert gh.run_cycle()["plugins"] == {}


def test_e2e_import_route_refuses_what_is_not_importable(admin, bench):
    gh = _gh()
    assert _import(admin, "x-v1.0.0").status_code == 400, "pas de source"
    bench.release("8.0.0-rc.1")
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    assert _import(admin, f"{COMPONENT}-v0.0.1").status_code == 404
    ignored = _import(admin, f"{COMPONENT}-v8.0.0-rc.1")
    assert ignored.status_code == 409 and "Hors du canal" in ignored.json()["detail"]
    assert admin.http.post(f"/admin/catalog/{admin.plugin_id}/github/import", json={}).status_code == 400

    # Sans session d'administration : pas d'import (le jeton de service n'ouvre pas cette route).
    admin.http.cookies.clear()
    anonymous = admin.http.post(f"/admin/catalog/{admin.plugin_id}/github/import", json={"tag": "x"},
                                headers={"x-admin-token": _TOKEN, "accept": "application/json"},
                                follow_redirects=False)
    assert anonymous.status_code in (401, 302, 303, 307, 503)


def test_e2e_deposit_of_another_plugin_is_not_accepted(admin, bench):
    """Un paquet tiré pour un plugin ne peut pas être envoyé comme version d'un autre."""
    gh = _gh()
    bench.release("9.1.0")
    assert _post_source(admin).status_code == 303
    gh.run_cycle()
    payload = _import(admin, f"{COMPONENT}-v9.1.0").json()
    res = admin.http.post("/admin/catalog/999999999/versions/upload", json={
        "upload_id": payload["upload_id"], "version": "9.1.0", "action": "publish_only",
        "signature": payload["signature"]}, follow_redirects=False)
    assert res.status_code == 400 and "autre plugin" in res.json()["detail"]
