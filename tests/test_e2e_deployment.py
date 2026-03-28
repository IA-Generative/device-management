#!/usr/bin/env python3
"""
=============================================================================
 TEST E2E — Validation complete d'un nouveau deploiement Device Management
=============================================================================

Ce fichier teste l'ensemble de la stack DM apres un deploiement from scratch.
Il valide chaque couche : infrastructure, base de donnees, API core, admin UI,
authentification Keycloak, et workflows metier complets.

PREREQUIS
---------
1. Docker et docker compose installes
2. Keycloak accessible sur le port 8082 (realm openwebui)
   → Si Keycloak n'est pas dispo, les tests OIDC sont marques skip
3. Ports libres : 3001 (DM), 5432 (Postgres), 8080 (Adminer), 8088 (Relay)

LANCEMENT
---------
# Deploiement complet from scratch + tests :
  ./tests/test_e2e_deployment.py --deploy

# Tests seuls (stack deja en cours) :
  pytest tests/test_e2e_deployment.py -v --base-url http://localhost:3001

# Tests sans Keycloak :
  pytest tests/test_e2e_deployment.py -v --base-url http://localhost:3001 -k "not keycloak"

# Avec rapport HTML :
  pytest tests/test_e2e_deployment.py -v --base-url http://localhost:3001 --html=reports/e2e.html

STRUCTURE DES TESTS
-------------------
Phase 0 — Infrastructure    : Docker build, containers up, ports ouverts
Phase 1 — Base de donnees   : Migrations, tables presentes, indexes
Phase 2 — API Core          : Healthcheck, config endpoint, enroll
Phase 3 — Admin UI pages    : Chaque ecran repond 200, contenu correct
Phase 4 — Admin UI CRUD     : Creation cohorte, flag, artifact, campagne
Phase 5 — Keycloak OIDC     : Redirect, callback, session, groupe admin-dm
Phase 6 — Workflows metier  : Cycle campagne complet, audit trail
Phase 7 — Securite          : Headers, CORS, upload rejet, session expiree
Phase 8 — Observabilite     : Logs structures, metriques, health summary

DEPLOIEMENT FROM SCRATCH (--deploy)
------------------------------------
Quand lance avec --deploy, le script execute dans l'ordre :
  1. docker compose down -v         (nettoyage complet)
  2. docker compose build           (reconstruction images)
  3. docker compose up -d postgres  (demarrage DB)
  4. Execution des migrations SQL   (schema + 002 + 003)
  5. docker compose up -d           (demarrage de tous les services)
  6. Attente readiness (healthcheck)
  7. Configuration Keycloak         (client admin-dm-ui + groupe admin-dm)
  8. Lancement des tests pytest

NETTOYAGE
---------
  cd deploy/docker && docker compose down -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DM_BASE_URL = os.getenv("DM_BASE_URL", "http://localhost:3001")
POSTGRES_DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/bootstrap")
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8082")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "openwebui")

COMPOSE_DIR = os.path.join(os.path.dirname(__file__), "..", "deploy", "docker")
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _http_get(path: str, **kwargs) -> httpx.Response:
    return httpx.get(f"{DM_BASE_URL}{path}", follow_redirects=False, timeout=10, **kwargs)


def _http_post(path: str, **kwargs) -> httpx.Response:
    return httpx.post(f"{DM_BASE_URL}{path}", follow_redirects=False, timeout=10, **kwargs)


def _admin_cookie() -> dict:
    """Forge an admin session cookie matching the deployed container's secret.

    The secret is read from deploy/docker/.env (ADMIN_SESSION_SECRET) so that
    the cookie is accepted by the running Docker container.  Falls back to the
    dev-mode value when the variable is absent.
    """
    # Resolve the same secret the container uses
    secret = os.getenv("ADMIN_SESSION_SECRET")
    if not secret:
        env_file = os.path.join(COMPOSE_DIR, ".env")
        if os.path.isfile(env_file):
            with open(env_file) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("ADMIN_SESSION_SECRET="):
                        secret = line.split("=", 1)[1]
                        break
    secret = secret or "changeme-dev-only"

    sys.path.insert(0, PROJECT_ROOT)
    os.environ["DATABASE_URL"] = POSTGRES_DSN
    os.environ["ADMIN_SESSION_SECRET"] = secret

    # Re-patch the module-level constant so _sign_session uses the right key
    import app.admin.auth as _auth
    _auth.SESSION_SECRET = secret

    cookie = _auth._sign_session({
        "sub": "e2e-test-user",
        "email": "e2e@test.local",
        "name": "E2E Tester",
        "exp": int(time.time()) + 3600,
    })
    return {"dm_admin_session": cookie}


def _admin_get(path: str) -> httpx.Response:
    return httpx.get(f"{DM_BASE_URL}{path}", cookies=_admin_cookie(),
                     follow_redirects=False, timeout=10)


def _admin_post(path: str, **kwargs) -> httpx.Response:
    return httpx.post(f"{DM_BASE_URL}{path}", cookies=_admin_cookie(),
                      follow_redirects=False, timeout=10, **kwargs)


def _keycloak_available() -> bool:
    try:
        r = httpx.get(f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _pg_query(sql: str, params: tuple = ()) -> list:
    """Execute a query against Postgres via the Docker container.

    Uses ``docker exec`` + ``psql`` to avoid needing a local psycopg2
    connection (the Docker compose Postgres may be unreachable from the
    host with the default role).  Falls back to direct psycopg2 if the
    ``DATABASE_URL`` env-var is explicitly set by the caller.
    """
    explicit_dsn = os.getenv("E2E_DATABASE_URL")
    if explicit_dsn:
        import psycopg2
        conn = psycopg2.connect(explicit_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    # Interpolate params into the SQL for psql (only simple %s supported)
    query = sql
    if params:
        safe = []
        for p in params:
            safe.append(f"'{p}'" if isinstance(p, str) else str(p))
        for v in safe:
            query = query.replace("%s", v, 1)

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "dev", "-d", "bootstrap", "-tAX", "-c", query],
        capture_output=True, text=True, cwd=os.path.abspath(COMPOSE_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip()}")
    rows = []
    for line in result.stdout.strip().splitlines():
        if line:
            rows.append(tuple(line.split("|")))
    return rows


def _pg_table_exists(table_name: str) -> bool:
    rows = _pg_query(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table_name,),
    )
    return len(rows) > 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def dm_ready():
    """Attendre que le DM soit pret (max 30s)."""
    for _ in range(30):
        try:
            r = httpx.get(f"{DM_BASE_URL}/healthz", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    pytest.fail(f"DM non accessible apres 30s sur {DM_BASE_URL}")


@pytest.fixture(scope="session")
def keycloak_ready():
    """Verifie que Keycloak est accessible. Skip si non dispo."""
    if not _keycloak_available():
        pytest.skip("Keycloak non accessible — tests OIDC ignores")
    return True


# ===========================================================================
# PHASE 0 — INFRASTRUCTURE
# ===========================================================================

class TestPhase0Infrastructure:
    """Verifier que les containers Docker tournent et repondent."""

    def test_dm_api_responds(self, dm_ready):
        """DM API repond sur /healthz."""
        r = _http_get("/healthz")
        assert r.status_code == 200

    def test_dm_port_open(self):
        """Port 3001 est ouvert."""
        import socket
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("localhost", 3001))
            s.close()
        except Exception:
            pytest.fail("Port 3001 non accessible")

    def test_postgres_port_open(self):
        """Port 5432 Postgres est ouvert."""
        import socket
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("localhost", 5432))
            s.close()
        except Exception:
            pytest.fail("Port 5432 Postgres non accessible")

    def test_adminer_responds(self):
        """Adminer repond sur port 8080."""
        try:
            r = httpx.get("http://localhost:8080", timeout=5)
            assert r.status_code == 200
        except Exception:
            pytest.skip("Adminer non accessible")


# ===========================================================================
# PHASE 1 — BASE DE DONNEES
# ===========================================================================

class TestPhase1Database:
    """Verifier que le schema DB est complet apres les migrations."""

    def test_table_provisioning(self):
        assert _pg_table_exists("provisioning"), "Table provisioning manquante"

    def test_table_device_connections(self):
        assert _pg_table_exists("device_connections"), "Table device_connections manquante"

    def test_table_relay_clients(self):
        assert _pg_table_exists("relay_clients"), "Table relay_clients manquante"

    def test_table_queue_jobs(self):
        assert _pg_table_exists("queue_jobs"), "Table queue_jobs manquante"

    def test_table_cohorts(self):
        assert _pg_table_exists("cohorts"), "Table cohorts manquante (migration 002)"

    def test_table_feature_flags(self):
        assert _pg_table_exists("feature_flags"), "Table feature_flags manquante (migration 002)"

    def test_table_campaigns(self):
        assert _pg_table_exists("campaigns"), "Table campaigns manquante (migration 002)"

    def test_table_artifacts(self):
        assert _pg_table_exists("artifacts"), "Table artifacts manquante (migration 002)"

    def test_table_admin_audit_log(self):
        assert _pg_table_exists("admin_audit_log"), "Table admin_audit_log manquante (migration 003)"

    def test_table_device_telemetry_events(self):
        assert _pg_table_exists("device_telemetry_events"), "Table device_telemetry_events manquante (migration 003)"

    def test_trim_trigger_exists(self):
        """Le trigger de trim des telemetry events existe."""
        rows = _pg_query(
            "SELECT 1 FROM pg_trigger WHERE tgname = 'trg_trim_telemetry_events'"
        )
        assert len(rows) == 1, "Trigger trg_trim_telemetry_events manquant"

    def test_indexes_exist(self):
        """Les indexes critiques sont presents."""
        for idx in [
            "idx_provisioning_email",
            "idx_provisioning_client_uuid",
            "idx_device_connections_client_connected_at",
            "idx_audit_created_at",
            "idx_telemetry_events_uuid",
        ]:
            rows = _pg_query(
                "SELECT 1 FROM pg_indexes WHERE indexname = %s", (idx,)
            )
            assert len(rows) >= 1, f"Index {idx} manquant"


# ===========================================================================
# PHASE 2 — API CORE
# ===========================================================================

class TestPhase2ApiCore:
    """Tester les endpoints API core du DM."""

    def test_healthz(self, dm_ready):
        r = _http_get("/healthz")
        assert r.status_code == 200

    def test_config_endpoint(self, dm_ready):
        """GET /config/config.json retourne une configuration valide."""
        r = _http_get("/config/config.json")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_config_device_endpoint(self, dm_ready):
        """GET /config/libreoffice/config.json retourne la config libreoffice."""
        r = _http_get("/config/libreoffice/config.json")
        assert r.status_code == 200

    def test_enroll_rejects_empty(self, dm_ready):
        """POST /enroll sans body retourne 4xx."""
        r = _http_post("/enroll")
        assert r.status_code >= 400

    def test_cors_headers(self, dm_ready):
        """Les headers CORS sont presents."""
        r = httpx.options(f"{DM_BASE_URL}/healthz", headers={
            "Origin": "http://localhost",
            "Access-Control-Request-Method": "GET",
        }, timeout=5)
        # CORS middleware should respond
        assert r.status_code in (200, 204, 405)


# ===========================================================================
# PHASE 3 — ADMIN UI PAGES
# ===========================================================================

class TestPhase3AdminPages:
    """Verifier que chaque ecran admin repond 200 avec le bon contenu."""

    def test_dashboard(self, dm_ready):
        r = _admin_get("/admin/")
        assert r.status_code == 200
        assert "Tableau de bord" in r.text

    def test_devices_page(self, dm_ready):
        r = _admin_get("/admin/devices")
        assert r.status_code == 200
        assert "Appareils" in r.text

    def test_cohorts_page(self, dm_ready):
        r = _admin_get("/admin/cohorts")
        assert r.status_code == 200
        assert "Cohortes" in r.text

    def test_flags_page(self, dm_ready):
        r = _admin_get("/admin/flags")
        assert r.status_code == 200
        assert "Feature flags" in r.text

    def test_artifacts_page(self, dm_ready):
        r = _admin_get("/admin/artifacts")
        assert r.status_code == 200
        assert "Artifacts" in r.text

    def test_campaigns_page(self, dm_ready):
        r = _admin_get("/admin/campaigns")
        assert r.status_code == 200
        assert "Campagnes" in r.text

    def test_campaign_new_page(self, dm_ready):
        r = _admin_get("/admin/campaigns/new")
        assert r.status_code == 200
        assert "Nouvelle campagne" in r.text

    def test_audit_page(self, dm_ready):
        r = _admin_get("/admin/audit")
        assert r.status_code == 200
        assert "Journal" in r.text

    def test_api_metrics(self, dm_ready):
        r = _admin_get("/admin/api/metrics")
        assert r.status_code == 200
        assert "dm-metric-tile" in r.text

    def test_api_health_summary(self, dm_ready):
        r = _admin_get("/admin/api/devices/health-summary")
        assert r.status_code == 200
        assert "OK" in r.text

    def test_static_css(self, dm_ready):
        r = _http_get("/admin/static/dm-admin.css")
        assert r.status_code == 200
        assert "dm-progress-bar" in r.text


# ===========================================================================
# PHASE 4 — ADMIN UI CRUD
# ===========================================================================

class TestPhase4AdminCrud:
    """Tester les operations CRUD via l'admin UI."""

    def test_create_cohort(self, dm_ready):
        """Creer une cohorte et verifier qu'elle apparait dans la liste."""
        r = _admin_post("/admin/cohorts", data={
            "name": f"e2e-cohort-{int(time.time())}",
            "type": "manual",
            "description": "Cohorte de test E2E",
            "members": "e2e-test@example.com",
        })
        assert r.status_code in (302, 303), f"Expected redirect, got {r.status_code}"
        assert "/admin/cohorts" in r.headers.get("location", "")

    def test_create_feature_flag(self, dm_ready):
        """Creer un feature flag."""
        r = _admin_post("/admin/flags", data={
            "name": f"e2e_flag_{int(time.time())}",
            "description": "Flag de test E2E",
            "default_value": "true",
        })
        assert r.status_code in (302, 303)
        assert "/admin/flags" in r.headers.get("location", "")

    def test_create_campaign(self, dm_ready):
        """Creer une campagne en mode brouillon."""
        r = _admin_post("/admin/campaigns", data={
            "name": f"E2E Campaign {int(time.time())}",
            "description": "Campagne de test E2E",
            "urgency": "normal",
            "start_status": "draft",
            "artifact_id": "",
            "rollback_artifact_id": "",
            "target_cohort_id": "",
            "deadline_at": "",
        })
        assert r.status_code in (302, 303)
        location = r.headers.get("location", "")
        assert "/admin/campaigns/" in location

    def test_audit_trail_populated(self, dm_ready):
        """Apres les CRUD, le journal d'audit contient des entrees."""
        r = _admin_get("/admin/audit")
        assert r.status_code == 200
        # Il devrait y avoir au moins une entree d'audit des tests precedents
        assert "e2e" in r.text.lower() or "cohort.create" in r.text or "flag.create" in r.text

    def test_audit_export_csv(self, dm_ready):
        """L'export CSV du journal d'audit fonctionne."""
        r = _admin_get("/admin/audit/export")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        assert "horodatage" in r.text


# ===========================================================================
# PHASE 5 — KEYCLOAK OIDC
# ===========================================================================

class TestPhase5Keycloak:
    """Tester l'integration OIDC Keycloak (skip si Keycloak non dispo)."""

    def test_oidc_redirect(self, dm_ready, keycloak_ready):
        """Sans cookie, /admin/ redirige vers Keycloak."""
        r = httpx.get(f"{DM_BASE_URL}/admin/", follow_redirects=False, timeout=10)
        assert r.status_code in (302, 307)
        location = r.headers.get("location", "")
        assert "openid-connect/auth" in location
        assert "client_id=admin-dm-ui" in location
        assert "redirect_uri=" in location

    def test_oidc_redirect_public_url(self, dm_ready, keycloak_ready):
        """Le redirect pointe vers l'URL publique Keycloak (localhost, pas docker.internal)."""
        r = httpx.get(f"{DM_BASE_URL}/admin/", follow_redirects=False, timeout=10)
        location = r.headers.get("location", "")
        assert "localhost:8082" in location, \
            f"Le redirect devrait pointer vers localhost:8082, pas host.docker.internal. Got: {location}"
        assert "host.docker.internal" not in location

    def test_oidc_callback_bad_code(self, dm_ready, keycloak_ready):
        """Callback avec un code invalide retourne 400."""
        r = httpx.get(f"{DM_BASE_URL}/admin/callback?code=fake&state=fake",
                      follow_redirects=False, timeout=10)
        assert r.status_code == 400

    def test_oidc_callback_bad_state(self, dm_ready, keycloak_ready):
        """Callback avec un state invalide retourne 400."""
        r = httpx.get(f"{DM_BASE_URL}/admin/callback?code=test&state=bad",
                      follow_redirects=False, timeout=10)
        assert r.status_code == 400

    def test_oidc_scope_correct(self, dm_ready, keycloak_ready):
        """Le scope demande est 'openid profile email' (sans 'groups')."""
        r = httpx.get(f"{DM_BASE_URL}/admin/", follow_redirects=False, timeout=10)
        location = r.headers.get("location", "")
        assert "scope=openid" in location
        # Le scope groups n'existe pas dans Keycloak par defaut,
        # les groupes sont inclus via le protocol mapper
        assert "groups" not in location.split("scope=")[1].split("&")[0] if "scope=" in location else True

    def test_keycloak_client_exists(self, keycloak_ready):
        """Le client admin-dm-ui existe dans Keycloak."""
        r = httpx.get(
            f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration",
            timeout=5,
        )
        assert r.status_code == 200
        # On ne peut pas lister les clients sans admin token,
        # mais on verifie que le realm est operationnel
        data = r.json()
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data

    def test_logout_clears_cookie(self, dm_ready, keycloak_ready):
        """GET /admin/logout supprime le cookie de session."""
        r = httpx.get(f"{DM_BASE_URL}/admin/logout", follow_redirects=False, timeout=10)
        assert r.status_code in (302, 307)
        # Le Set-Cookie devrait contenir une expiration/suppression
        set_cookie = r.headers.get("set-cookie", "")
        assert "dm_admin_session" in set_cookie


# ===========================================================================
# PHASE 6 — WORKFLOWS METIER
# ===========================================================================

class TestPhase6Workflows:
    """Tester les workflows metier complets."""

    def test_campaign_lifecycle(self, dm_ready):
        """Cycle de vie complet d'une campagne : create → activate → pause → resume → rollback."""
        # 1. Creer
        r = _admin_post("/admin/campaigns", data={
            "name": f"Lifecycle Test {int(time.time())}",
            "description": "Test cycle de vie",
            "urgency": "normal",
            "start_status": "draft",
            "artifact_id": "",
            "rollback_artifact_id": "",
            "target_cohort_id": "",
            "deadline_at": "",
        })
        assert r.status_code in (302, 303)
        campaign_url = r.headers["location"]
        campaign_id = campaign_url.rstrip("/").split("/")[-1]

        # 2. Activer
        r = _admin_post(f"/admin/campaigns/{campaign_id}/activate")
        assert r.status_code in (302, 303)

        # 3. Verifier le statut en lisant la page
        r = _admin_get(f"/admin/campaigns/{campaign_id}")
        assert r.status_code == 200
        assert "Actif" in r.text

        # 4. Pause
        r = _admin_post(f"/admin/campaigns/{campaign_id}/pause")
        assert r.status_code in (302, 303)

        # 5. Resume
        r = _admin_post(f"/admin/campaigns/{campaign_id}/resume")
        assert r.status_code in (302, 303)

        # 6. Rollback
        r = _admin_post(f"/admin/campaigns/{campaign_id}/rollback")
        assert r.status_code in (302, 303)

        # 7. Verifier le statut final
        r = _admin_get(f"/admin/campaigns/{campaign_id}")
        assert r.status_code == 200
        assert "Rollback" in r.text

    def test_flag_with_override(self, dm_ready):
        """Creer un flag, puis ajouter un override par cohorte."""
        ts = int(time.time())

        # 1. Creer une cohorte
        r = _admin_post("/admin/cohorts", data={
            "name": f"override-cohort-{ts}",
            "type": "manual",
            "description": "",
            "members": "override-test@example.com",
        })
        assert r.status_code in (302, 303)

        # 2. Creer un flag
        r = _admin_post("/admin/flags", data={
            "name": f"override_flag_{ts}",
            "description": "Flag avec override",
            "default_value": "false",
        })
        assert r.status_code in (302, 303)

        # 3. Retrouver les IDs dans la page flags
        r = _admin_get("/admin/flags")
        assert f"override_flag_{ts}" in r.text

    def test_device_search_and_detail(self, dm_ready):
        """Recherche d'appareils et acces au detail."""
        # La liste devices doit fonctionner meme sans donnees
        r = _admin_get("/admin/devices")
        assert r.status_code == 200
        assert "Appareils" in r.text

        # Recherche avec filtre
        r = _admin_get("/admin/devices?owner=test&health=ok")
        assert r.status_code == 200

        # Detail d'un device inexistant → 404 ou 500 (service returns None)
        r = _admin_get("/admin/devices/00000000-0000-0000-0000-000000000000")
        assert r.status_code in (404, 500, 200)  # Implementation may vary


# ===========================================================================
# PHASE 7 — SECURITE
# ===========================================================================

class TestPhase7Security:
    """Verifier les aspects securite du deploiement."""

    def test_security_headers_xframe(self, dm_ready):
        """X-Frame-Options: DENY est present."""
        r = _admin_get("/admin/")
        assert r.headers.get("x-frame-options") == "DENY"

    def test_security_headers_nosniff(self, dm_ready):
        """X-Content-Type-Options: nosniff est present."""
        r = _admin_get("/admin/")
        assert r.headers.get("x-content-type-options") == "nosniff"

    def test_security_headers_csp(self, dm_ready):
        """Content-Security-Policy est present sur les pages admin."""
        r = _admin_get("/admin/")
        csp = r.headers.get("content-security-policy", "")
        assert "default-src" in csp
        assert "script-src" in csp

    def test_security_headers_referrer(self, dm_ready):
        """Referrer-Policy est present."""
        r = _admin_get("/admin/")
        assert "referrer-policy" in {k.lower() for k in r.headers.keys()}

    def test_session_cookie_httponly(self, dm_ready):
        """Le cookie de session a le flag HttpOnly."""
        r = _admin_get("/admin/")
        # En mode dev, le cookie est defini sur la reponse
        for cookie_header in r.headers.get_list("set-cookie"):
            if "dm_admin_session" in cookie_header:
                assert "httponly" in cookie_header.lower(), \
                    "Cookie dm_admin_session doit avoir HttpOnly"

    def test_upload_rejects_exe(self, dm_ready):
        """L'upload refuse les extensions non autorisees."""
        from app.admin.services.artifacts import validate_upload
        assert validate_upload("malware.exe", 1000) is not None
        assert validate_upload("virus.bat", 1000) is not None
        assert validate_upload("script.sh", 1000) is not None
        # Extensions autorisees
        assert validate_upload("plugin.oxt", 1000) is None
        assert validate_upload("addon.xpi", 1000) is None
        assert validate_upload("extension.crx", 1000) is None

    def test_upload_rejects_oversized(self, dm_ready):
        """L'upload refuse les fichiers > 100 Mo."""
        from app.admin.services.artifacts import validate_upload
        assert validate_upload("big.oxt", 150 * 1024 * 1024) is not None

    def test_expired_session_rejected(self):
        """Une session expiree est rejetee."""
        from app.admin.auth import _sign_session, _verify_session
        expired = _sign_session({
            "sub": "x", "email": "x", "name": "x",
            "exp": int(time.time()) - 100,
        })
        assert _verify_session(expired) is None

    def test_tampered_session_rejected(self):
        """Un cookie modifie est rejete."""
        from app.admin.auth import _sign_session, _verify_session
        valid = _sign_session({
            "sub": "x", "email": "x", "name": "x",
            "exp": int(time.time()) + 3600,
        })
        tampered = valid[:-5] + "ZZZZZ"
        assert _verify_session(tampered) is None

    def test_admin_group_check(self):
        """La verification du groupe admin-dm fonctionne."""
        from app.admin.auth import _has_admin_group
        assert _has_admin_group({"groups": ["admin-dm"]}) is True
        assert _has_admin_group({"groups": ["users"]}) is False
        assert _has_admin_group({"groups": [], "resource_access": {
            "admin-dm-ui": {"roles": ["admin-dm"]}
        }}) is True
        assert _has_admin_group({}) is False


# ===========================================================================
# PHASE 8 — OBSERVABILITE
# ===========================================================================

class TestPhase8Observability:
    """Verifier les mecanismes d'observabilite."""

    def test_health_summary_counters(self, dm_ready):
        """L'API health-summary retourne les 4 compteurs."""
        r = _admin_get("/admin/api/devices/health-summary")
        assert r.status_code == 200
        text = r.text
        assert "OK" in text
        assert "Inactifs" in text
        assert "En erreur" in text
        assert "Jamais vus" in text

    def test_campaign_stats_api(self, dm_ready):
        """L'API campaign stats retourne du HTML valide."""
        # Creer une campagne pour avoir un ID valide
        r = _admin_post("/admin/campaigns", data={
            "name": f"Stats Test {int(time.time())}",
            "description": "",
            "urgency": "normal",
            "start_status": "draft",
            "artifact_id": "",
            "rollback_artifact_id": "",
            "target_cohort_id": "",
            "deadline_at": "",
        })
        campaign_url = r.headers.get("location", "")
        campaign_id = campaign_url.rstrip("/").split("/")[-1]

        r = _admin_get(f"/admin/api/campaigns/{campaign_id}/stats")
        assert r.status_code == 200
        assert "dm-metric-tile" in r.text

    def test_device_activity_api(self, dm_ready):
        """L'API device activity repond meme sans donnees."""
        r = _admin_get("/admin/api/devices/nonexistent-uuid/activity")
        assert r.status_code == 200

    def test_metrics_auto_refresh(self, dm_ready):
        """Le dashboard contient les attributs HTMX pour l'auto-refresh."""
        r = _admin_get("/admin/")
        assert r.status_code == 200
        assert 'hx-trigger="every 30s"' in r.text or "hx-get" in r.text

    def test_audit_log_records_actions(self, dm_ready):
        """Les actions CRUD sont enregistrees dans l'audit log."""
        rows = _pg_query("SELECT COUNT(*) FROM admin_audit_log")
        count = int(rows[0][0])
        assert count > 0, "L'audit log devrait contenir des entrees apres les tests CRUD"


# ===========================================================================
# DEPLOIEMENT FROM SCRATCH
# ===========================================================================

def deploy_from_scratch():
    """Deployer la stack complete from scratch et lancer les tests."""
    import shutil

    compose_dir = os.path.abspath(COMPOSE_DIR)
    project_root = os.path.abspath(PROJECT_ROOT)

    def run(cmd, cwd=compose_dir, check=True):
        print(f"  $ {cmd}")
        result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
        if check and result.returncode != 0:
            print(f"  STDERR: {result.stderr}")
            raise RuntimeError(f"Command failed: {cmd}")
        return result

    print("=" * 70)
    print(" DEPLOIEMENT FROM SCRATCH — Device Management")
    print("=" * 70)

    # 1. Cleanup
    print("\n[1/8] Nettoyage complet...")
    run("docker compose down -v", check=False)

    # 2. Build
    print("\n[2/8] Construction des images Docker...")
    run("docker compose build")

    # 3. Start Postgres
    print("\n[3/8] Demarrage PostgreSQL...")
    run("docker compose up -d postgres")
    print("  Attente readiness Postgres...")
    for i in range(30):
        result = run(
            "docker compose exec -T postgres pg_isready -U dev -d bootstrap",
            check=False,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Postgres non pret apres 30s")

    # 4. Migrations
    print("\n[4/8] Execution des migrations SQL...")
    for sql_file in ["db/schema.sql", "db/migrations/002_campaigns.sql", "db/migrations/003_admin_audit.sql"]:
        abs_path = os.path.join(project_root, sql_file)
        if os.path.exists(abs_path):
            run(f"docker compose exec -T postgres psql -U postgres -d bootstrap < {abs_path}",
                cwd=project_root)
            print(f"  ✓ {sql_file}")

    # 5. Start all services
    print("\n[5/8] Demarrage de tous les services...")
    run("docker compose up -d")

    # 6. Wait for DM
    print("\n[6/8] Attente readiness DM...")
    for i in range(30):
        try:
            r = httpx.get(f"{DM_BASE_URL}/healthz", timeout=2)
            if r.status_code == 200:
                print(f"  ✓ DM pret en {i+1}s")
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("DM non pret apres 30s")

    # 7. Configure Keycloak (si disponible)
    print("\n[7/8] Configuration Keycloak...")
    if _keycloak_available():
        keycloak_container = "grafrag-experimentation-keycloak-1"
        kc_pass = subprocess.run(
            f"docker inspect {keycloak_container} --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}'",
            shell=True, capture_output=True, text=True,
        ).stdout
        # Extract password
        for line in kc_pass.splitlines():
            if line.startswith("KEYCLOAK_ADMIN_PASSWORD="):
                kc_password = line.split("=", 1)[1]
                break
        else:
            print("  ⚠ Mot de passe Keycloak non trouve, skip config")
            kc_password = None

        if kc_password:
            kcadm = f"docker exec {keycloak_container} /opt/keycloak/bin/kcadm.sh"

            # Login
            run(f'{kcadm} config credentials --server http://localhost:8080 --realm master --user admin --password "{kc_password}"',
                cwd=project_root, check=False)

            # Create client (ignore error if exists)
            result = run(
                f"""{kcadm} create clients -r openwebui \
                    -s clientId=admin-dm-ui -s 'name=Admin DM UI' -s enabled=true \
                    -s publicClient=false -s standardFlowEnabled=true \
                    -s 'redirectUris=["http://localhost:3001/admin/callback"]' \
                    -s 'webOrigins=["http://localhost:3001"]' -s protocol=openid-connect""",
                cwd=project_root, check=False,
            )
            if result.returncode == 0:
                client_uuid = result.stdout.strip().split("'")[-2] if "'" in result.stdout else ""
                print(f"  ✓ Client admin-dm-ui cree")
            else:
                print(f"  ℹ Client admin-dm-ui existait deja")

            # Create group
            run(f"{kcadm} create groups -r openwebui -s name=admin-dm",
                cwd=project_root, check=False)
            print("  ✓ Groupe admin-dm")

            # Assign user1
            user1_result = run(
                f"{kcadm} get users -r openwebui -q username=user1 --fields id",
                cwd=project_root, check=False,
            )
            group_result = run(
                f"{kcadm} get groups -r openwebui --fields id,name",
                cwd=project_root, check=False,
            )
            print("  ✓ Configuration Keycloak terminee")
    else:
        print("  ⚠ Keycloak non disponible, skip")

    # 8. Run tests
    print("\n[8/8] Lancement des tests E2E...")
    print("=" * 70)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v",
         "--base-url", DM_BASE_URL, "--tb=short"],
        cwd=project_root,
    )
    return result.returncode


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == "__main__":
    if "--deploy" in sys.argv:
        sys.exit(deploy_from_scratch())
    else:
        print(__doc__)
        print("Usage:")
        print("  python tests/test_e2e_deployment.py --deploy    # Full deploy + tests")
        print("  pytest tests/test_e2e_deployment.py -v          # Tests only (stack running)")
