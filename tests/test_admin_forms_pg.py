"""Intégration (vrai Postgres) — ce que l'écran d'envoi d'une version enregistre vraiment.

L'écran « Nouvelle version » passait le gabarit de configuration du paquet
(`dm-config.json`) à la base sous forme de dictionnaire : l'écriture échouait,
l'erreur n'était que journalisée, et le gabarit n'était jamais enregistré. Seul
l'envoi par l'API l'enregistrait.

Marqué `integration`. Même montage que tests/test_deploy_guidance_pg.py.
"""
# ruff: noqa: F811 — les fixtures importées sont redéclarées en paramètres de test
from __future__ import annotations

import io
import json
import zipfile

import pytest

from tests.test_deploy_guidance_pg import (  # noqa: F401 — fixtures pytest
    _SLUG,
    _TOKEN,
    _db_url,
    mod,
    site,
)

pytestmark = pytest.mark.integration


def _package(template: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("manifest.json", (2026, 1, 1, 0, 0, 0)), '{"name": "it"}')
        zf.writestr(zipfile.ZipInfo("dm-config.json", (2026, 1, 1, 0, 0, 0)), json.dumps(template))
    return buf.getvalue()


def test_e2e_version_upload_stores_the_embedded_config_template(site):
    template = {"configVersion": 1, "default": {"reglage": "du plugin"}, "prod": {"autre": 1}}
    res = site.http.post(
        f"/admin/catalog/{site.plugin_id}/versions/upload",
        data={"version": "1.0.0", "action": "publish_only", "environment": "it"},
        files={"binary": (f"{_SLUG}-1.0.0.xpi", _package(template), "application/octet-stream")},
        headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 303, res.text
    stored = site.sql("SELECT config_template FROM plugins WHERE id = %s", (site.plugin_id,))[0][0]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored is not None, "le gabarit du paquet doit être enregistré"
    assert stored["default"] == {"reglage": "du plugin"} and stored["prod"]["autre"] == 1
    # Les réglages que le DM gouverne sont complétés par leur repère.
    assert stored["prod"]["keycloakClientId"] == "${{KEYCLOAK_CLIENT_ID}}"
