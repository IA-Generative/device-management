"""Formulaires de l'administration envoyés par `fetch` : la redirection doit être lisible.

Avec `redirect: 'manual'`, la réponse à une redirection est opaque : son adresse
est illisible, et le script rechargeait la page courante. L'onglet d'arrivée et
les bandeaux de retour (« liste de DM réécrite », issue #133) étaient perdus.
Défaut invisible sans navigateur : ce contrôle le garde hors du dépôt.
"""
from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "app" / "admin" / "templates"


def test_no_admin_template_follows_redirects_by_hand():
    offenders = [p.name for p in sorted(TEMPLATES.glob("*.html"))
                 if re.search(r"redirect\s*:\s*['\"]manual['\"]", p.read_text(encoding="utf-8"))]
    assert offenders == [], f"redirection opaque dans : {offenders}"


def test_forms_navigate_to_where_the_server_sent_them():
    for name in ("base.html", "catalog_plugin.html", "catalog_plugin_new.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "resp.redirected" in text and "resp.url" in text, name
