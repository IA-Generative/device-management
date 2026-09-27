"""Convention ADR-0004 MirAI next (format Dockerflow) : /app/version.json servi sur /__version__.

- Hors image, la route répond « dev » avec les six clés — jamais une erreur.
- Dans l'image, elle rend le fichier tel quel.
- Elle reste servie quand la barrière de configuration est active (sinon 503 au démarrage),
  et elle est filtrée des logs d'accès comme les sondes.
- Le script qui écrit le fichier ne garde que la section de CETTE version du CHANGELOG.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.runtime_config as rc
from app import logfilters
from app import main as app_main
from app import version as app_version
from app.main import app

RACINE = Path(__file__).resolve().parents[1]
SCRIPT = RACINE / "scripts" / "version_json.py"
SIX_CLES = {"source", "version", "commit", "build", "code_date", "changes"}

CHANGELOG = """# Changelog

## [0.9.19](https://github.com/IA-Generative/device-management/compare/v0.9.18...v0.9.19) (2026-09-28)

### Features

* l'image dit quelle version elle est

## [0.9.18] (2026-09-20)

### Bug Fixes

* ancienne version
"""


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _gate_off():
    rc.disable_request_gate()
    rc._config_ready = False
    yield
    rc.disable_request_gate()
    rc._config_ready = False


# ── la route ─────────────────────────────────────────────────────────────────

def test_hors_image_la_route_repond_dev(client):
    r = client.get("/__version__")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    corps = r.json()
    assert set(corps) == SIX_CLES
    assert corps["version"] == "dev" and corps["changes"] == []


def test_sert_le_fichier_de_l_image_tel_quel(client, tmp_path, monkeypatch):
    fichier = tmp_path / "version.json"
    fichier.write_text(json.dumps({"version": "0.9.19", "commit": "abc", "changes": ["x"]}), encoding="utf-8")
    monkeypatch.setattr(app_version, "VERSION_JSON", fichier)
    assert client.get("/__version__").json() == {"version": "0.9.19", "commit": "abc", "changes": ["x"]}


def test_un_fichier_illisible_donne_dev_pas_une_erreur(client, tmp_path, monkeypatch):
    fichier = tmp_path / "version.json"
    fichier.write_text("{pas du json", encoding="utf-8")
    monkeypatch.setattr(app_version, "VERSION_JSON", fichier)
    assert client.get("/__version__").json()["version"] == "dev"


def test_la_route_passe_la_barriere_de_configuration(client):
    rc.enable_request_gate()  # config jamais chargée : le métier répond 503, pas la version
    assert client.get("/__version__").status_code == 200


def test_la_route_est_hors_du_schema_openapi(client):
    assert "/__version__" not in client.get("/openapi.json").json()["paths"]


def test_la_route_est_dans_chaque_liste_de_chemins():
    assert "/__version__" in logfilters.PROBE_PATHS
    assert "/__version__" in app_main._LLM_MODE_ALLOWED_PREFIXES
    assert "/__version__" in app_main._CONFIG_GATE_EXEMPT_PREFIXES


# ── le script qui écrit le fichier ───────────────────────────────────────────

def _ecrire(tmp_path, version, changelog=CHANGELOG):
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    sortie = tmp_path / "version.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--version", version, "--commit", "abc1234",
         "--build", "https://ci.example.invalid/1", "--code-date", "2026-09-28T10:00:00Z",
         "--source", "https://github.com/IA-Generative/device-management",
         "--changelog", str(tmp_path / "CHANGELOG.md"), "--out", str(sortie)],
        check=True,
    )
    return json.loads(sortie.read_text(encoding="utf-8"))


def test_script_ne_garde_que_la_section_de_cette_version(tmp_path):
    v = _ecrire(tmp_path, "0.9.19")
    assert set(v) == SIX_CLES
    assert v["version"] == "0.9.19" and v["commit"] == "abc1234"
    texte = "\n".join(v["changes"])
    assert "quelle version elle est" in texte
    assert "ancienne version" not in texte


def test_script_sans_journal_ce_n_est_pas_une_erreur(tmp_path):
    assert _ecrire(tmp_path, "0.9.19", changelog="")["changes"] == []
    assert _ecrire(tmp_path, "9.9.9")["changes"] == []
