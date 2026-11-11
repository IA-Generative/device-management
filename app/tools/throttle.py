"""Débit par personne et par service : fenêtre fixe en mémoire (DM_TOOLS_RATE_PER_MINUTE,
0 = désactivé). Mémoire locale au pod : la borne est approximative en multi-réplicas,
ce qui suffit à une protection ; les amonts gardent leur propre 429 (Retry-After respecté)."""
from __future__ import annotations

import math
import threading
import time

from .. import runtime_config

_lock = threading.Lock()
_compteurs: dict[tuple[str, str, int], int] = {}


def limite_par_minute() -> int:
    try:
        return max(0, int(runtime_config.cfg("DM_TOOLS_RATE_PER_MINUTE", 60) or 0))
    except (TypeError, ValueError):
        return 60


def autoriser(sujet: str, service: str, *, now: float | None = None) -> tuple[bool, int]:
    """Rend (autorisé, retry_after_s). Compte aussi les refus (fenêtre fixe standard)."""
    limite = limite_par_minute()
    if limite <= 0 or not sujet:
        return True, 0
    now = time.time() if now is None else now
    fenetre = int(now // 60)
    cle = (sujet, service, fenetre)
    with _lock:
        _compteurs[cle] = _compteurs.get(cle, 0) + 1
        n = _compteurs[cle]
        for k in [k for k in _compteurs if k[2] < fenetre]:
            del _compteurs[k]
    retry_after = max(1, int(math.ceil((fenetre + 1) * 60 - now)))
    return n <= limite, retry_after


def reset_for_tests() -> None:
    with _lock:
        _compteurs.clear()
