"""Intégration (vrai Postgres) — un artefact appartient à son plugin (issue #62).

Deux plugins du catalogue partagent un device_type. Avant, la clé d'un artefact
était (device_type, platform_variant, version) : le second plugin qui publiait le
même numéro était refusé, ou remplaçait sur confirmation le binaire du premier,
que les postes du premier recevaient alors.

Couvre :
  - le rattachement de l'artefact à son plugin, et sa reprise s'il n'en a pas ;
  - le même numéro publié par les deux plugins, chacun avec son binaire ;
  - la garde du double envoi, qui ne vaut plus qu'au sein d'un plugin ;
  - les fichiers stockés, distincts d'un plugin à l'autre ;
  - une campagne sans plugin, servie aux seuls postes du propriétaire de l'artefact ;
  - l'initialisation de artifacts.plugin_id sur une base existante.

Exécution : `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bootstrap \\
             pytest tests/test_artifact_plugin_pg.py -m integration`
"""
from __future__ import annotations

import io
import json
import os
import sys
import types
import uuid
import zipfile

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "db", "schema.sql")

_DEVICE_TYPE = "it-shared-type"
_SLUG_A = "it-owner-a"
_SLUG_B = "it-owner-b"
_TOKEN = "it-artifact-plugin-token"


@pytest.fixture(scope="module")
def _db_url() -> str:
    pytest.importorskip("psycopg2")
    url = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini")
    import psycopg2
    try:
        psycopg2.connect(url, connect_timeout=3).close()
    except Exception as exc:  # pragma: no cover — dépend de l'infra locale
        pytest.skip(f"Postgres injoignable: {exc}")
    from app.services.db import apply_schema
    apply_schema(url, _SCHEMA_PATH)
    return url


def _connect(db_url):
    import psycopg2
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    return conn


def _sql(db_url, query, params=()):
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _purge(db_url) -> None:
    conn = _connect(db_url)
    with conn.cursor() as cur:
        cur.execute("UPDATE plugins SET general_version_id = NULL WHERE slug IN (%s, %s)", (_SLUG_A, _SLUG_B))
        cur.execute("""DELETE FROM campaigns WHERE plugin_id IN (SELECT id FROM plugins WHERE slug IN (%s, %s))
                       OR artifact_id IN (SELECT id FROM artifacts WHERE device_type = %s)""",
                    (_SLUG_A, _SLUG_B, _DEVICE_TYPE))
        cur.execute("DELETE FROM plugin_installations WHERE plugin_id IN "
                    "(SELECT id FROM plugins WHERE slug IN (%s, %s))", (_SLUG_A, _SLUG_B))
        cur.execute("DELETE FROM plugin_versions WHERE plugin_id IN "
                    "(SELECT id FROM plugins WHERE slug IN (%s, %s))", (_SLUG_A, _SLUG_B))
        cur.execute("DELETE FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,))
        cur.execute("DELETE FROM cohorts WHERE name LIKE %s", (f"auto-{_DEVICE_TYPE}-%",))
        cur.execute("DELETE FROM plugins WHERE slug IN (%s, %s)", (_SLUG_A, _SLUG_B))
    conn.close()


def _create_plugins(db_url) -> tuple[int, int]:
    conn = _connect(db_url)
    with conn.cursor() as cur:
        ids = []
        for slug in (_SLUG_A, _SLUG_B):
            cur.execute("INSERT INTO plugins (slug, name, device_type, status) "
                        "VALUES (%s, %s, %s, 'active') RETURNING id", (slug, slug, _DEVICE_TYPE))
            ids.append(cur.fetchone()[0])
    conn.close()
    return ids[0], ids[1]


# ═════════════════════════════════════════════════════════════════════════════
# Service — dans une transaction annulée
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def svc(_db_url):
    import psycopg2

    from app.admin.services import artifacts
    conn = psycopg2.connect(_db_url)
    cur = conn.cursor()
    ids = []
    for slug in (_SLUG_A, _SLUG_B):
        cur.execute("INSERT INTO plugins (slug, name, device_type, status) "
                    "VALUES (%s, %s, %s, 'active') RETURNING id", (slug, slug, _DEVICE_TYPE))
        ids.append(cur.fetchone()[0])
    try:
        yield types.SimpleNamespace(cur=cur, a=ids[0], b=ids[1], artifacts=artifacts)
    finally:
        conn.rollback()
        conn.close()


def _create(svc, plugin_id, checksum, version="1.0.0"):
    return svc.artifacts.create_artifact(
        svc.cur, device_type=_DEVICE_TYPE, platform_variant="", version=version,
        s3_path=f"{_DEVICE_TYPE}/{plugin_id}-{version}.xpi", checksum=checksum,
        plugin_id=plugin_id)


def _owner(svc, artifact_id):
    svc.cur.execute("SELECT plugin_id, checksum FROM artifacts WHERE id = %s", (artifact_id,))
    return svc.cur.fetchone()


def test_artifact_belongs_to_the_plugin_that_published_it(svc):
    artifact_id = _create(svc, svc.a, "sha256:aa")
    assert _owner(svc, artifact_id) == (svc.a, "sha256:aa")


def test_republishing_replaces_the_plugin_own_artifact(svc):
    first = _create(svc, svc.a, "sha256:aa")
    again = _create(svc, svc.a, "sha256:a2")
    assert again == first
    assert _owner(svc, first) == (svc.a, "sha256:a2")


def test_same_number_from_the_other_plugin_is_not_a_double_upload(svc):
    _create(svc, svc.a, "sha256:aa")
    # Sans confirmation : ce n'est pas le même plugin qui renvoie son numéro.
    assert svc.artifacts.check_overwrite(svc.cur, device_type=_DEVICE_TYPE, platform_variant="",
                                         version="1.0.0", checksum="sha256:bb",
                                         plugin_id=svc.b) is False


def test_each_plugin_gets_its_own_artifact_for_the_same_number(svc):
    first = _create(svc, svc.a, "sha256:aa")
    second = _create(svc, svc.b, "sha256:bb")
    assert second != first
    assert _owner(svc, first) == (svc.a, "sha256:aa")
    assert _owner(svc, second) == (svc.b, "sha256:bb")


def test_key_is_plugin_type_variant_number(svc):
    svc.cur.execute("SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indrelid = 'artifacts'::regclass AND indisunique AND NOT indisprimary")
    [(definition,)] = svc.cur.fetchall()
    assert "(plugin_id, device_type, platform_variant, version)" in definition


def test_artifact_without_owner_is_taken_over_by_the_first_publisher(svc):
    svc.cur.execute("INSERT INTO artifacts (device_type, platform_variant, version, checksum) "
                    "VALUES (%s, '', '1.0.0', 'sha256:old') RETURNING id", (_DEVICE_TYPE,))
    orphan = svc.cur.fetchone()[0]
    assert svc.artifacts.check_overwrite(svc.cur, device_type=_DEVICE_TYPE, platform_variant="",
                                         version="1.0.0", checksum="sha256:new",
                                         confirmed=True, plugin_id=svc.b) is True
    assert _create(svc, svc.b, "sha256:new") == orphan
    assert _owner(svc, orphan) == (svc.b, "sha256:new")


def test_same_binary_or_other_number_still_pass(svc):
    _create(svc, svc.a, "sha256:aa")
    check = svc.artifacts.check_overwrite
    assert check(svc.cur, device_type=_DEVICE_TYPE, platform_variant="", version="1.0.0",
                 checksum="sha256:aa", plugin_id=svc.a) is False
    assert check(svc.cur, device_type=_DEVICE_TYPE, platform_variant="", version="1.0.1",
                 checksum="sha256:bb", plugin_id=svc.b) is False


def test_plugin_of_an_artifact_is_its_owner(svc):
    from app.admin.services.campaigns import plugin_id_for_artifact
    artifact_id = _create(svc, svc.b, "sha256:bb")
    # Deux plugins actifs du même type : sans propriétaire, impossible de trancher.
    assert plugin_id_for_artifact(svc.cur, artifact_id) == svc.b


# ═════════════════════════════════════════════════════════════════════════════
# Initialisation de la colonne sur une base existante
# ═════════════════════════════════════════════════════════════════════════════

def test_schema_upgrade_backfills_only_certain_owners(_db_url):
    """Base d'avant la colonne : schéma appliqué, colonne retirée, données posées,
    schéma réappliqué. Seuls les propriétaires certains sont renseignés."""
    from urllib.parse import urlsplit, urlunsplit

    from app.services.db import apply_schema
    # Une base à part, pas un schéma : les correctifs d'apply_schema lisent
    # information_schema sur toute la base.
    name = f"it_backfill_{uuid.uuid4().hex[:8]}"
    _sql(_db_url, f"CREATE DATABASE {name}")
    url = urlunsplit(urlsplit(_db_url)._replace(path=f"/{name}"))
    try:
        apply_schema(url, _SCHEMA_PATH)
        conn = _connect(url)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE artifacts DROP COLUMN plugin_id")

            def plugin(slug, device_type):
                cur.execute("INSERT INTO plugins (slug, name, device_type) VALUES (%s, %s, %s) "
                            "RETURNING id", (slug, slug, device_type))
                return cur.fetchone()[0]

            def artifact(device_type, version):
                cur.execute("INSERT INTO artifacts (device_type, platform_variant, version) "
                            "VALUES (%s, '', %s) RETURNING id", (device_type, version))
                return cur.fetchone()[0]

            a = plugin("a", "shared")
            b = plugin("b", "shared")
            solo = plugin("solo", "lonely")
            by_version = artifact("shared", "1.0")
            cur.execute("INSERT INTO plugin_versions (plugin_id, version, artifact_id) "
                        "VALUES (%s, '1.0', %s)", (a, by_version))
            by_campaign = artifact("shared", "2.0")
            cur.execute("INSERT INTO campaigns (name, type, artifact_id, plugin_id) "
                        "VALUES ('c', 'plugin_update', %s, %s)", (by_campaign, b))
            disputed = artifact("shared", "3.0")
            cur.execute("INSERT INTO plugin_versions (plugin_id, version, artifact_id) "
                        "VALUES (%s, '3.0', %s), (%s, '3.0', %s)", (a, disputed, b, disputed))
            unreferenced_shared = artifact("shared", "4.0")
            unreferenced_solo = artifact("lonely", "1.0")
        conn.close()

        apply_schema(url, _SCHEMA_PATH)
        owners = dict(_sql(url, "SELECT id, plugin_id FROM artifacts"))
        assert owners[by_version] == a
        assert owners[by_campaign] == b
        assert owners[disputed] is None
        assert owners[unreferenced_shared] is None
        assert owners[unreferenced_solo] == solo
    finally:
        _sql(_db_url, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# ═════════════════════════════════════════════════════════════════════════════
# De bout en bout, par les routes HTTP
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def mod(_db_url, tmp_path_factory):
    """app.main avec le VRAI psycopg2, binaires dans un dossier temporaire."""
    binaries = str(tmp_path_factory.mktemp("binaries"))
    os.environ["DATABASE_URL"] = _db_url
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DM_QUEUE_ADMIN_TOKEN"] = _TOKEN
    os.environ["DM_RUNTIME_MODE"] = "all"
    os.environ["DM_BINARIES_MODE"] = "local"
    os.environ["DM_LOCAL_BINARIES_DIR"] = binaries
    os.environ["DM_ADMIN_INTERNAL_URL"] = "http://127.0.0.1:9"
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    current = sys.modules.get("psycopg2")
    if not (isinstance(current, types.ModuleType) and getattr(current, "__file__", None)):
        sys.modules.pop("psycopg2", None)
    import psycopg2
    for name in [n for n in sys.modules if n == "app.main" or n == "app.settings"
                 or n.startswith("app.admin")]:
        sys.modules.pop(name, None)
    import app.main as m
    m.psycopg2 = psycopg2
    m._binaries_dir_for_tests = binaries
    return m


def _xpi(marker: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("manifest.json", (2026, 1, 1, 0, 0, 0)), '{"name": "it"}')
        zf.writestr(zipfile.ZipInfo("marker.txt", (2026, 1, 1, 0, 0, 0)), marker)
    return buf.getvalue()


@pytest.fixture
def site(mod, _db_url):
    from fastapi.testclient import TestClient
    _purge(_db_url)
    a, b = _create_plugins(_db_url)
    http = TestClient(mod.app, client=("127.0.0.1", 50162))

    def deploy(slug, version, marker, filename="plugin.xpi", **fields):
        return http.post(
            f"/api/plugins/{slug}/deploy",
            data={"version": version, "strategy": "immediate", **fields},
            files={"binary": (filename, _xpi(marker), "application/octet-stream")},
            headers={"X-Admin-Token": _TOKEN})

    def config_update(slug, plugin_version):
        res = http.get(f"/config/{slug}/config.json", headers={
            "X-Plugin-Version": plugin_version, "X-Platform-Type": "thunderbird",
            "X-Platform-Version": "140.0", "X-Client-UUID": str(uuid.uuid4())})
        assert res.status_code == 200, res.text
        return res.json()["update"]

    try:
        yield types.SimpleNamespace(http=http, a=a, b=b, deploy=deploy, config_update=config_update,
                                    sql=lambda q, p=(): _sql(_db_url, q, p), mod=mod)
    finally:
        _purge(_db_url)
        root = os.path.join(mod._binaries_dir_for_tests, _DEVICE_TYPE)
        if os.path.isdir(root):
            for name in os.listdir(root):
                os.remove(os.path.join(root, name))


def _stored_marker(site, slug, version) -> str:
    """Contenu du binaire que l'artefact de ce plugin sert pour ce numéro."""
    rows = site.sql("SELECT a.s3_path FROM artifacts a JOIN plugins p ON p.id = a.plugin_id "
                    "WHERE p.slug = %s AND a.version = %s", (slug, version))
    assert len(rows) == 1, rows
    path = os.path.join(site.mod._binaries_dir_for_tests, _DEVICE_TYPE, os.path.basename(rows[0][0]))
    with zipfile.ZipFile(path) as zf:
        return zf.read("marker.txt").decode()


def test_e2e_two_plugins_publish_the_same_number_each_with_its_binary(site):
    """Le cas de l'issue : même type, même numéro, deux binaires, sans confirmation."""
    assert site.deploy(_SLUG_A, "2.0.0", "binaire-a").status_code == 201
    second = site.deploy(_SLUG_B, "2.0.0", "binaire-b")
    assert second.status_code == 201, second.text
    owners = sorted(site.sql("SELECT plugin_id FROM artifacts WHERE device_type = %s AND version = '2.0.0'",
                             (_DEVICE_TYPE,)))
    assert owners == sorted([(site.a,), (site.b,)])
    # Chaque plugin sert son binaire, et ses postes reçoivent la mise à jour.
    assert _stored_marker(site, _SLUG_A, "2.0.0") == "binaire-a"
    assert _stored_marker(site, _SLUG_B, "2.0.0") == "binaire-b"
    assert site.config_update(_SLUG_A, "1.0.0")["target_version"] == "2.0.0"
    assert site.config_update(_SLUG_B, "1.0.0")["target_version"] == "2.0.0"
    # Même nom d'envoi (« plugin.xpi ») : fichiers distincts, au nom du plugin.
    stored = sorted(os.listdir(os.path.join(site.mod._binaries_dir_for_tests, _DEVICE_TYPE)))
    assert stored == [f"2.0.0_{_SLUG_A}-plugin.xpi", f"2.0.0_{_SLUG_B}-plugin.xpi"]


def test_e2e_double_upload_guard_still_applies_within_a_plugin(site):
    assert site.deploy(_SLUG_A, "2.0.0", "binaire-a").status_code == 201
    assert site.deploy(_SLUG_B, "2.0.0", "binaire-b").status_code == 201
    refused = site.deploy(_SLUG_B, "2.0.0", "binaire-b2")
    assert refused.status_code == 409 and refused.json()["code"] == "version_exists"
    assert site.deploy(_SLUG_B, "2.0.0", "binaire-b2", overwrite="true").status_code == 201
    assert _stored_marker(site, _SLUG_A, "2.0.0") == "binaire-a"
    assert _stored_marker(site, _SLUG_B, "2.0.0") == "binaire-b2"


def test_e2e_campaign_without_plugin_reaches_only_the_artifact_owner(site):
    assert site.deploy(_SLUG_A, "2.0.0", "binaire-a").status_code == 201
    # Campagne d'avant le rattachement au plugin : plugin_id NULL.
    site.sql("UPDATE campaigns SET plugin_id = NULL WHERE plugin_id = %s", (site.a,))
    assert site.config_update(_SLUG_A, "1.0.0")["target_version"] == "2.0.0"
    assert site.config_update(_SLUG_B, "1.0.0") is None


def _admin_session(site):
    import time

    from app.admin.auth import SESSION_COOKIE, _sign_session
    site.http.cookies.set(SESSION_COOKIE, _sign_session({
        "sub": "it-62", "email": "admin@test.invalid", "name": "IT 62",
        "exp": int(time.time()) + 600}))


def _package_with_config(marker: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("manifest.json", (2026, 1, 1, 0, 0, 0)), '{"name": "it"}')
        zf.writestr(zipfile.ZipInfo("marker.txt", (2026, 1, 1, 0, 0, 0)), marker)
        zf.writestr(zipfile.ZipInfo("dm-config.json", (2026, 1, 1, 0, 0, 0)),
                    json.dumps({"configVersion": 1, "default": {"marque": marker}}))
    return buf.getvalue()


def test_e2e_wizard_asks_which_plugin_then_touches_only_that_one(site):
    """Assistant de déploiement : deux plugins actifs de ce type, il faut choisir.
    Le modèle de configuration n'est écrit que dans la fiche choisie."""
    _admin_session(site)
    page = site.http.get("/admin/deploy")
    assert page.status_code == 200 and f'data-slug="{_SLUG_B}"' in page.text

    def create(**fields):
        data = {"device_type": _DEVICE_TYPE, "version": "3.0.0", "deploy_type": "immediate",
                "target_mode": "all", **fields}
        return site.http.post("/admin/deploy/create", data=data, follow_redirects=False, files={
            "binary": ("paquet.xpi", _package_with_config("pour-b"), "application/octet-stream")})

    refused = create()
    assert refused.status_code == 400
    assert "choisissez le plugin" in refused.json()["detail"]
    assert create(plugin_id=str(site.a + site.b + 1000)).status_code == 400

    assert create(plugin_id=str(site.b)).status_code == 303
    assert site.sql("SELECT plugin_id FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,)) \
        == [(site.b,)]
    assert site.sql("SELECT plugin_id FROM campaigns WHERE artifact_id IN "
                    "(SELECT id FROM artifacts WHERE device_type = %s)", (_DEVICE_TYPE,)) == [(site.b,)]
    templates = dict(site.sql("SELECT slug, config_template IS NOT NULL FROM plugins "
                              "WHERE slug IN (%s, %s)", (_SLUG_A, _SLUG_B)))
    assert templates == {_SLUG_A: False, _SLUG_B: True}
    assert sorted(os.listdir(os.path.join(site.mod._binaries_dir_for_tests, _DEVICE_TYPE))) \
        == [f"{_SLUG_B}-3.0.0.xpi"]
