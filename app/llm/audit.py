"""Journal d'audit structuré du proxy LLM — une ligne JSON par requête, sur stdout.

Champs : trace_id (propagé de bout en bout), identité résolue (email,
client_uuid), modèle, backend, verdicts du pipeline (guardrails/quota), statut,
latence, usage tokens si le backend le fournit. JAMAIS de secret ni de contenu
de prompt/réponse en clair — prêt pour une exportation SIEM/OpenTelemetry.
"""
from __future__ import annotations

import json
import logging

from .pipeline import LlmRequestContext

logger = logging.getLogger("dm-llm-audit")


def log_request(
    ctx: LlmRequestContext,
    *,
    status: int,
    duration_seconds: float,
    error_kind: str | None = None,
    usage: dict | None = None,
) -> None:
    entry = {
        "event": "llm.request",
        "trace_id": ctx.trace_id,
        "route": ctx.route,
        "email": ctx.identity.email,
        "client_uuid": ctx.identity.client_uuid,
        "auth_method": ctx.identity.auth_method,
        "model": ctx.model or None,
        "backend": ctx.backend_name or None,
        "stream": ctx.stream,
        "status": status,
        "latency_ms": round(duration_seconds * 1000, 1),
        "verdicts": ctx.verdicts or None,
        "quota": ctx.meta.get("quota"),
        "error": error_kind,
    }
    if isinstance(usage, dict):
        entry["usage"] = {
            k: usage.get(k)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            if k in usage
        }
    try:
        logger.info(json.dumps({k: v for k, v in entry.items() if v is not None},
                               ensure_ascii=False, sort_keys=True))
    except Exception:  # pragma: no cover - l'audit ne doit jamais casser la requête
        logger.exception("llm audit log failed")
