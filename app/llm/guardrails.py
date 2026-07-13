"""Guardrails enfichables du proxy LLM — hook entrée (prompt) ET sortie (réponse).

Interface unique : ``Guardrail.check(payload, direction, ctx)`` retourne
allow | deny | transform. Les règles actives et leur ORDRE viennent de la clé de
configuration hot-reloadable ``LLM_GUARDRAILS`` (CSV ou JSON de noms, relue à
chaque requête) : brancher une vraie règle = enregistrer une classe dans
``GUARDRAIL_REGISTRY`` + la nommer dans la config — zéro modification du cœur.

Livrés :
- ``noop``     : allow systématique (défaut) — matérialise le point d'accroche.
- ``deny_all`` : refuse tout (kill-switch d'urgence pilotable à chaud depuis
  l'onglet Config admin, et preuve testée qu'un deny se branche par config).

Limite streaming : en sortie streamée l'inspection est best-effort par chunk
(cf. pipeline.on_chunk) ; le verdict complet ne s'applique qu'aux réponses non
streamées.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi.responses import JSONResponse

from .. import runtime_config
from .errors import LlmProxyError, openai_error
from .pipeline import Interceptor, LlmRequestContext

logger = logging.getLogger("device-management.llm")


class Direction(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"


@dataclass
class GuardrailResult:
    action: str            # "allow" | "deny" | "transform"
    payload: Any = None    # payload transformé si action == "transform"
    reason: str = ""


class Guardrail:
    name = "guardrail"

    def check(self, payload: Any, direction: Direction, ctx: LlmRequestContext) -> GuardrailResult:
        raise NotImplementedError


class NoopGuardrail(Guardrail):
    """Pass-through : matérialise l'interface, n'altère rien."""

    name = "noop"

    def check(self, payload: Any, direction: Direction, ctx: LlmRequestContext) -> GuardrailResult:
        return GuardrailResult(action="allow")


class DenyAllGuardrail(Guardrail):
    """Kill-switch : refuse toute requête (activable à chaud par config)."""

    name = "deny_all"

    def check(self, payload: Any, direction: Direction, ctx: LlmRequestContext) -> GuardrailResult:
        return GuardrailResult(action="deny", reason="blocked by policy (deny_all)")


# Registre nom → classe. Une règle future (anti-injection, PII…) s'ajoute ici
# (ou via un plugin qui l'enregistre) puis s'active par la config LLM_GUARDRAILS.
GUARDRAIL_REGISTRY: dict[str, type[Guardrail]] = {
    NoopGuardrail.name: NoopGuardrail,
    DenyAllGuardrail.name: DenyAllGuardrail,
}


def _configured_names() -> list[str]:
    raw = str(runtime_config.cfg("LLM_GUARDRAILS", "") or "").strip()
    if not raw:
        return [NoopGuardrail.name]
    if raw.startswith("["):
        try:
            return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
        except Exception:
            logger.warning("LLM_GUARDRAILS: JSON invalide, fallback noop")
            return [NoopGuardrail.name]
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_guardrails() -> list[Guardrail]:
    """Instancie les guardrails actifs, dans l'ordre de la config (hot-reload)."""
    guardrails: list[Guardrail] = []
    for name in _configured_names():
        cls = GUARDRAIL_REGISTRY.get(name)
        if cls is None:
            logger.warning("Guardrail inconnu ignoré: %s", name)
            continue
        guardrails.append(cls())
    return guardrails


class GuardrailInterceptor(Interceptor):
    """Adapte 1..n Guardrail au pipeline (entrée via before, sortie via after)."""

    name = "guardrail"

    def __init__(self, guardrails: list[Guardrail] | None = None):
        self.guardrails = guardrails if guardrails is not None else build_guardrails()

    async def before(self, ctx: LlmRequestContext) -> JSONResponse | None:
        payload = ctx.payload
        for guardrail in self.guardrails:
            result = guardrail.check(payload, Direction.REQUEST, ctx)
            if result.action == "deny":
                ctx.verdicts.append(f"guardrail:{guardrail.name}:deny")
                return openai_error(
                    403,
                    result.reason or "Request blocked by guardrail.",
                    err_type="invalid_request_error",
                    code="content_policy_violation",
                )
            if result.action == "transform" and result.payload is not None:
                ctx.verdicts.append(f"guardrail:{guardrail.name}:transform")
                payload = result.payload
        ctx.payload = payload
        return None

    async def after(self, ctx: LlmRequestContext, body: dict) -> dict:
        for guardrail in self.guardrails:
            result = guardrail.check(body, Direction.RESPONSE, ctx)
            if result.action == "deny":
                ctx.verdicts.append(f"guardrail:{guardrail.name}:deny_response")
                raise LlmProxyError(
                    403,
                    result.reason or "Response blocked by guardrail.",
                    err_type="invalid_request_error",
                    code="content_policy_violation",
                )
            if result.action == "transform" and result.payload is not None:
                ctx.verdicts.append(f"guardrail:{guardrail.name}:transform_response")
                body = result.payload
        return body
