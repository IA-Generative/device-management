"""Journal du relais d'outils : une ligne JSON par appel, sur le logger dm-tools-audit.

Champs : service, outil, statut, durée, taille de la réponse, identité (client_uuid,
sujet haché court), code d'erreur. JAMAIS les arguments, la réponse ni le jeton :
une requête de recherche ou le corps d'un mail n'ont rien à faire dans un journal.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger("dm-tools-audit")


def sujet_court(sub: str) -> str:
    """Empreinte courte du sub : corrèle sans identifier en clair."""
    if not sub:
        return ""
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:12]


def log_appel(*, trace_id: str, service: str, outil: str, status: int, duration_ms: float,
              size: int, client_uuid: str, sub: str, acts: bool, error: str | None = None,
              truncated: bool = False) -> None:
    entry = {
        "event": "tools.call",
        "trace_id": trace_id,
        "service": service,
        "tool": outil,
        "acts": acts,
        "status": status,
        "latency_ms": round(duration_ms, 1),
        "size": size,
        "client_uuid": client_uuid or None,
        "sub": sujet_court(sub) or None,
        "error": error,
        "truncated": truncated or None,
    }
    try:
        logger.info(json.dumps({k: v for k, v in entry.items() if v is not None},
                               ensure_ascii=False, sort_keys=True))
    except Exception:  # pragma: no cover
        logger.exception("tools audit log failed")
