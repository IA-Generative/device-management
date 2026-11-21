"""Page de simulation d'un système d'information : `GET /simulation/si/{systeme}`.

Le plugin Éloignement ouvre, pour chaque système configuré dans `eloignementRecherches`,
une adresse où `{q}`, `{nom}`, `{prenom}` et `{naissance}` sont remplacés par la recherche de
l'agent. Tant que les vrais systèmes ne sont pas reliés, cette page tient leur place : elle
affiche la requête reçue et quelques faux résultats, marqués comme tels.

Page statique, sans script ni ressource externe. Tout ce qui vient de la requête est
échappé avant affichage. Servie seulement si `DM_SIMULATION_SI_ENABLED` est vrai (défaut :
faux) : une page de démonstration n'a rien à faire sur un DM de production.
"""
from __future__ import annotations

import re
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from . import runtime_config

router = APIRouter()

_NOM_SYSTEME = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
_LONGUEUR_MAX = 200
_MENTION = "simulation, données fictives"

_FAUX_RESULTATS = (
    ("EXEMPLE Camille", "01/01/1970", "SIMULATION-0001"),
    ("SPECIMEN Dominique", "02/02/1980", "SIMULATION-0002"),
    ("FICTIF Claude", "03/03/1990", "SIMULATION-0003"),
)

_STYLE = (
    "body{font-family:sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1e1e1e}"
    ".bandeau{background:#fff4d6;border:1px solid #b88a00;padding:.6rem .8rem}"
    "dt{font-weight:bold}dd{margin:0 0 .5rem 0}"
    ".resultat{border:1px solid #ccc;padding:.6rem .8rem;margin:.6rem 0}"
    ".mention{color:#8a5a00;font-size:.85rem}"
)


def _actif() -> bool:
    raw = str(runtime_config.cfg("DM_SIMULATION_SI_ENABLED", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _affichable(valeur: str) -> str:
    """Valeur reçue, coupée puis échappée (guillemets compris) pour être posée dans la page."""
    return escape(valeur[:_LONGUEUR_MAX], quote=True)


@router.get("/simulation/si/{systeme}", include_in_schema=False)
def simulation_si(systeme: str, q: str = "", nom: str = "", prenom: str = "", naissance: str = "") -> HTMLResponse:
    if not _actif() or not _NOM_SYSTEME.match(systeme):
        raise HTTPException(status_code=404, detail="Not Found")
    recu = "".join(
        f"<dt>{libelle}</dt><dd>{_affichable(valeur) or '<em>(vide)</em>'}</dd>"
        for libelle, valeur in (("Recherche", q), ("Nom", nom), ("Prénom", prenom), ("Naissance", naissance))
    )
    resultats = "".join(
        f'<div class="resultat"><strong>{identite}</strong>, né(e) le {ne_le}<br>'
        f'Fiche {fiche} <span class="mention">({_MENTION})</span></div>'
        for identite, ne_le, fiche in _FAUX_RESULTATS
    )
    page = (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        f"<title>Simulation {_affichable(systeme)}</title><style>{_STYLE}</style></head><body>"
        f"<h1>Système « {_affichable(systeme)} »</h1>"
        f'<p class="bandeau">Page de {_MENTION}. Aucun système réel n\'est interrogé, '
        "rien de ce qui est affiché ici ne concerne une personne réelle.</p>"
        f"<h2>Requête reçue</h2><dl>{recu}</dl>"
        f"<h2>Résultats</h2>{resultats}"
        "</body></html>"
    )
    return HTMLResponse(page, headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
    })
