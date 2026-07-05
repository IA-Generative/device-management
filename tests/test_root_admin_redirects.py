"""Redirections racine + base admin, correctes derrière un reverse-proxy.

Contexte : l'app tourne derrière un proxy (OpenShift Routes / nginx) qui termine
le TLS ; le pod ne voit que du HTTP interne. Les redirections doivent utiliser un
``Location`` en CHEMIN ABSOLU (sans scheme/host) pour ne jamais reconstruire un
``http://<interne>/…`` cassé.

(A) `/admin` (sans slash)  → 307 `/admin/`   — app en mode admin (router monté).
(B) `/`                    → 307 `/catalog/` — app principale (rôle api/all).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://dev:dev@localhost:5432/bootstrap")


def _admin_app() -> FastAPI:
    """App minimale montant le router admin sous /admin — comme le pod admin
    (`include_router(prefix="/admin")`), sans dépendre du runtime_mode global."""
    from app.admin.router import router as admin_router

    a = FastAPI()
    a.include_router(admin_router, prefix="/admin")
    return a


# ─── (B) Racine → catalogue ───────────────────────────────────────────────

def test_root_redirects_to_catalog_307():
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 307  # 307 : préserve la méthode
    assert r.headers["location"] == "/catalog/"


def test_root_redirect_location_has_no_scheme_behind_proxy():
    """X-Forwarded-Proto=https : le Location reste un chemin absolu (pas d'http://)."""
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    r = client.get("/", headers={
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "assistant.exemple.gouv.fr",
    })
    loc = r.headers["location"]
    assert loc == "/catalog/"
    assert "://" not in loc  # ni http:// ni https:// : chemin relatif à l'hôte


# ─── (A) Base admin sans slash → avec slash ───────────────────────────────

def test_admin_base_redirects_to_slash_307():
    client = TestClient(_admin_app(), follow_redirects=False)
    r = client.get("/admin")
    assert r.status_code == 307
    assert r.headers["location"] == "/admin/"


def test_admin_base_redirect_no_scheme_behind_proxy():
    client = TestClient(_admin_app(), follow_redirects=False)
    r = client.get("/admin", headers={"X-Forwarded-Proto": "https"})
    loc = r.headers["location"]
    assert loc == "/admin/"
    assert "://" not in loc


# ─── Non-régression : routes existantes toujours enregistrées ─────────────

def test_existing_routes_still_registered():
    """Les nouveaux handlers n'écrasent pas /catalog ni le dashboard /admin/."""
    from app.main import app

    main_paths = {getattr(r, "path", None) for r in app.routes}
    assert "/catalog" in main_paths        # route catalogue inchangée
    assert "/" in main_paths               # nouveau handler racine

    from app.admin.router import router as admin_router

    admin_paths = {getattr(r, "path", None) for r in admin_router.routes}
    assert "/" in admin_paths              # dashboard (monté en /admin/)
    assert "" in admin_paths               # nouveau redirect base (/admin)


def test_admin_base_redirect_does_not_require_auth():
    """Le redirect de base ne doit PAS exiger d'auth : il pointe vers /admin/,
    c'est là que l'auth s'applique. Donc /admin → 307 direct (pas 401/302 login)."""
    client = TestClient(_admin_app(), follow_redirects=False)
    r = client.get("/admin")
    assert r.status_code == 307
