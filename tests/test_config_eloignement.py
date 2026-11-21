"""Clés de configuration du plugin Éloignement (contrat, section 4) : déclarées dans un
gabarit de profil, elles arrivent telles quelles dans la réponse de /config. Le DM n'en
porte aucune valeur : absentes du gabarit, elles sont absentes de la réponse.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.llm_utils import enroll, load_module

CLES_ELOIGNEMENT = {
    "eloignementRecherches": [
        {"id": "fpr", "libelle": "Fichier simulé",
         "url": "${{PUBLIC_BASE_URL}}/simulation/si/fpr?q={q}&nom={nom}&prenom={prenom}&naissance={naissance}"},
    ],
    "eloignementOcrModele": "modele-de-lecture",
    "eloignementOcrConsigne": "Transcris le document.",
    "eloignementOcrService": "ocr",
    "eloignementClassementConsigne": "Donne le type du document.",
    "eloignementDecoupeService": "decoupe",
    "eloignementDecoupeSeuilPages": 12,
    "eloignementInterfaceUrl": "https://interface.example",
}
URL_RESOLUE = ("https://example.test/bootstrap/simulation/si/fpr"
               "?q={q}&nom={nom}&prenom={prenom}&naissance={naissance}")


def _client(tmp_path) -> TestClient:
    """Gabarits de fichier : le profil « int » déclare les clés, le profil par défaut non."""
    base = {"configVersion": 1, "config": {"enabled": True}}
    (tmp_path / "config.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "config.int.json").write_text(
        json.dumps({"configVersion": 1, "config": {"enabled": True, **CLES_ELOIGNEMENT}}), encoding="utf-8")
    return TestClient(load_module({"DM_CONFIG_DIR": str(tmp_path)}).app)


def test_cles_d_un_gabarit_de_profil_servies_telles_quelles(tmp_path):
    client = _client(tmp_path)
    res = client.get("/config/thunderbird/config.json?profile=int", headers=enroll(client))
    assert res.status_code == 200, res.text
    config = res.json()["config"]
    attendu = {**CLES_ELOIGNEMENT,
               "eloignementRecherches": [{"id": "fpr", "libelle": "Fichier simulé", "url": URL_RESOLUE}]}
    assert {cle: config.get(cle) for cle in CLES_ELOIGNEMENT} == attendu


def test_cles_servies_aussi_a_un_poste_sans_paire_relais(tmp_path):
    """Ce ne sont pas des secrets : la purge des secrets d'un appel anonyme n'y touche pas."""
    config = _client(tmp_path).get("/config/thunderbird/config.json?profile=int").json()["config"]
    assert config["eloignementRecherches"][0]["url"] == URL_RESOLUE
    assert config["eloignementDecoupeSeuilPages"] == 12


def test_cles_absentes_si_le_gabarit_ne_les_declare_pas(tmp_path):
    client = _client(tmp_path)
    config = client.get("/config/thunderbird/config.json?profile=prod", headers=enroll(client)).json()["config"]
    assert not [cle for cle in config if cle.startswith("eloignement")]


def test_gabarit_de_paquet_profil_sur_defaut():
    """Format dm-config.json (gabarit en base) : la section du profil complète `default`."""
    from app.main import _build_config_from_template, _substitute_env
    gabarit = {"configVersion": 1,
               "default": {"eloignementOcrService": "ocr", "eloignementDecoupeSeuilPages": 20},
               "int": {**CLES_ELOIGNEMENT, "eloignementOcrService": "ocr-int"}}
    config_int = _substitute_env(_build_config_from_template(gabarit, "int"))["config"]
    assert config_int["eloignementOcrService"] == "ocr-int"
    assert config_int["eloignementDecoupeSeuilPages"] == 12
    assert config_int["eloignementRecherches"][0]["url"] == URL_RESOLUE
    config_prod = _build_config_from_template(gabarit, "prod")["config"]
    assert config_prod == {"eloignementOcrService": "ocr", "eloignementDecoupeSeuilPages": 20}
