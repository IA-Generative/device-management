"""Sonde de la chaîne du relais servi par l'api : api → proxy de sortie → upstream.

En production le relais nginx n'est pas déployé : c'est l'api qui sert /relay-assistant/*
(app.main.relay_assistant_proxy) avec httpx et trust_env=True, donc PAR le proxy de
sortie d'entreprise (ORION au DGX, 10.224.32.127 en prod). Savoir si le pod nginx vit
ne dit rien de cette chaîne ; cette sonde refait exactement le même appel, avec la même
pile, sur chaque cible configurée, et dit où ça casse : proxy, DNS, TLS, délai, HTTP.

Règles :
- une cible est CONFIGURÉE si sa variable RELAY_*_UPSTREAM est posée ; les autres sont
  simplement absentes du résultat (pas une erreur) ;
- une réponse HTTP, même 401/403/404, prouve que la chaîne va jusqu'à l'upstream : ok ;
  un 5xx est une erreur de l'upstream ou du proxy ;
- le résultat est gardé quelques secondes (`CACHE_S`) : la route /ops/relay/probe est
  publique comme /ops/health/full, et personne ne doit pouvoir faire marteler le proxy
  en la rechargeant ;
- rien de secret ne sort : l'URL de la cible n'est pas renvoyée, seul l'hôte du proxy
  (sans identifiants) l'est, pour que le voyant dise « par quel chemin ».
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

# (cible, variable d'environnement, chemin sondé). Même liste que _RELAY_UPSTREAM_MAP
# dans app.main — un relais ajouté là se sonde ici.
CIBLES: tuple[tuple[str, str, str], ...] = (
    ("keycloak", "RELAY_KEYCLOAK_UPSTREAM", "/.well-known/openid-configuration"),
    ("compte-rendu", "RELAY_COMPTE_RENDU_UPSTREAM", ""),
    ("mcr-api", "RELAY_MCR_API_UPSTREAM", ""),
)

TIMEOUT_S = 4.0
CACHE_S = 15.0

_cache_lock = threading.Lock()
_cache: dict = {"a": 0.0, "resultat": None}


def tls_verify() -> bool:
    """Même règle que app.main._TLS_VERIFY (DM_TLS_VERIFY=false réservé aux satellites)."""
    return os.getenv("DM_TLS_VERIFY", "true").strip().lower() not in ("false", "0", "no", "off")


def cibles_configurees() -> dict[str, str]:
    """cible → URL sondée, pour les seules cibles dont l'upstream est posé."""
    out: dict[str, str] = {}
    for cible, var, chemin in CIBLES:
        base = (os.getenv(var) or "").strip().rstrip("/")
        if base:
            out[cible] = f"{base}{chemin}"
    return out


def proxy_sortant() -> str | None:
    """hôte:port du proxy HTTPS de sortie (sans identifiants), ou None en direct."""
    brut = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()
    if not brut:
        return None
    try:
        p = urlsplit(brut if "://" in brut else f"http://{brut}")
        return p.hostname + (f":{p.port}" if p.port else "") if p.hostname else None
    except ValueError:
        return None


def _classer(exc: Exception) -> str:
    """Un mot sur la cause, à l'intention de l'exploitant, puis le message tronqué."""
    msg = str(exc)[:90] or type(exc).__name__
    if isinstance(exc, httpx.ProxyError):
        return f"proxy : {msg}"
    if isinstance(exc, httpx.TimeoutException):
        return f"délai dépassé ({TIMEOUT_S:g} s) : {msg}"
    if isinstance(exc, httpx.ConnectError):
        bas = msg.lower()
        if "ssl" in bas or "certificate" in bas or "tls" in bas:
            return f"TLS : {msg}"
        return f"connexion ou DNS : {msg}"
    return f"{type(exc).__name__} : {msg}"


def sonder(cible: str, url: str, client_factory=None) -> dict:
    """Un GET sur l'URL de la cible, avec la pile du relais (trust_env=True).

    Retourne {"status": "ok"|"error", "latency_ms": int, "detail": str, "http": int|None}.
    `client_factory` ne sert qu'aux tests.
    """
    via = proxy_sortant()
    chemin = f" par le proxy {via}" if via else " en direct"
    factory = client_factory or (lambda: httpx.Client(trust_env=True, timeout=TIMEOUT_S, verify=tls_verify()))
    t0 = time.monotonic()
    try:
        with factory() as client:
            r = client.get(url)
        ms = round((time.monotonic() - t0) * 1000)
        if r.status_code >= 500:
            return {"status": "error", "latency_ms": ms, "http": r.status_code,
                    "detail": f"HTTP {r.status_code}{chemin} — l'upstream ou le proxy répond en erreur"}
        return {"status": "ok", "latency_ms": ms, "http": r.status_code,
                "detail": f"HTTP {r.status_code}{chemin}"}
    except Exception as exc:  # toute cause réseau devient un détail lisible, jamais une 500
        ms = round((time.monotonic() - t0) * 1000)
        return {"status": "error", "latency_ms": ms, "http": None, "detail": f"{_classer(exc)}{chemin}"}


def sonder_toutes(force: bool = False, client_factory=None) -> dict:
    """Toutes les cibles configurées, en parallèle, résultat gardé CACHE_S secondes.

    Retourne {"checked_at": epoch, "proxy": hôte|None, "cibles": {cible: résultat}}.
    """
    with _cache_lock:
        if not force and _cache["resultat"] is not None and time.monotonic() - _cache["a"] < CACHE_S:
            return _cache["resultat"]
    cibles = cibles_configurees()
    resultats: dict[str, dict] = {}
    if cibles:
        with ThreadPoolExecutor(max_workers=len(cibles)) as pool:
            futs = {c: pool.submit(sonder, c, u, client_factory) for c, u in cibles.items()}
            for c, f in futs.items():
                resultats[c] = f.result()
    resultat = {"checked_at": time.time(), "proxy": proxy_sortant(), "cibles": resultats}
    with _cache_lock:
        _cache["a"] = time.monotonic()
        _cache["resultat"] = resultat
    return resultat


def vider_cache() -> None:
    with _cache_lock:
        _cache["a"] = 0.0
        _cache["resultat"] = None
