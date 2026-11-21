"""Page de simulation d'un système d'information (`/simulation/si/{systeme}`) : sert de
cible aux recherches du plugin Éloignement tant que les vrais systèmes ne sont pas reliés.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.llm_utils import load_module


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DM_SIMULATION_SI_ENABLED", "true")
    return TestClient(load_module().app)


def test_page_affiche_la_requete_recue_et_des_resultats_fictifs(client):
    res = client.get("/simulation/si/fpr", params={"q": "dossier 12", "nom": "Durand", "prenom": "Camille",
                                                      "naissance": "1980-02-29"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    page = res.text
    for attendu in ("fpr", "dossier 12", "Durand", "Camille", "1980-02-29"):
        assert attendu in page, attendu
    assert page.count("simulation, données fictives") >= 3   # le bandeau et chaque faux résultat
    assert page.count('class="resultat"') in (2, 3)


def test_page_sans_parametre_reste_lisible(client):
    res = client.get("/simulation/si/agdref")
    assert res.status_code == 200
    assert "simulation, données fictives" in res.text


def test_tout_parametre_affiche_est_echappe(client):
    charge = "<script>alert(1)</script>\"'><img src=x onerror=alert(2)>&amp;"
    res = client.get("/simulation/si/fpr", params={"q": charge, "nom": charge, "prenom": charge,
                                                      "naissance": charge})
    assert res.status_code == 200
    page = res.text
    assert "<script" not in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot;&#x27;&gt;" in page
    assert "&amp;amp;" in page
    # Sans script ni ressource externe, et interdit de l'être.
    assert res.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-robots-tag"] == "noindex, nofollow"


def test_parametre_trop_long_est_coupe(client):
    res = client.get("/simulation/si/fpr", params={"q": "a" * 5000})
    assert res.status_code == 200
    assert "a" * 201 not in res.text and "a" * 200 in res.text


def test_nom_de_systeme_hors_gabarit_refuse(client):
    for systeme in ("FPR", "a" * 31, "fpr%3Cscript%3E", "fpr.html", "%20"):
        res = client.get(f"/simulation/si/{systeme}")
        assert res.status_code == 404, systeme
        assert "<script" not in res.text


@pytest.mark.parametrize("valeur", [None, "false", ""])
def test_page_absente_si_la_simulation_n_est_pas_activee(monkeypatch, valeur):
    monkeypatch.delenv("DM_SIMULATION_SI_ENABLED", raising=False)
    if valeur is not None:
        monkeypatch.setenv("DM_SIMULATION_SI_ENABLED", valeur)
    assert TestClient(load_module().app).get("/simulation/si/fpr").status_code == 404
