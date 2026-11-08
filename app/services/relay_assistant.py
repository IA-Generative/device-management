"""Le relais nginx « relay-assistant » : est-il déployé, et où le sonder ?

Une seule règle pour les trois lecteurs de DM_RELAY_ASSISTANT_URL (bandeau d'état de
l'admin, page /admin/debug, /ops/health/full) :

- variable ABSENTE : défaut docker-compose, le relais est un conteneur voisin ;
- variable présente mais VIDE : le chart n'a pas déployé le relais (relayAssistant.enabled
  à false, cas de la production, où l'api sert /relay-assistant/* elle-même) — ce n'est
  pas une panne, on ne le sonde pas et on ne le compte pas en erreur ;
- variable posée : on sonde {url}/healthz.

Vu en production le 2026-11-08 : les trois lecteurs prenaient la chaîne vide pour une
URL et appelaient urlopen("/healthz") — « unknown url type: '/healthz' », un service
« dégradé » en permanence pour une absence voulue.
"""
from __future__ import annotations

import os

DEFAUT_COMPOSE = "http://relay-assistant"


def base_url() -> str | None:
    """URL de base du relais sans « / » final, ou None s'il n'est pas déployé."""
    url = os.getenv("DM_RELAY_ASSISTANT_URL", DEFAUT_COMPOSE).strip().rstrip("/")
    return url or None


def healthz_url() -> str | None:
    """URL de la sonde du relais, ou None s'il n'est pas déployé."""
    base = base_url()
    return f"{base}/healthz" if base else None
