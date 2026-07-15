"""Auth entrante duale du proxy LLM.

Ordre de résolution (le comportement exact du plugin figé étant incertain, on
accepte les deux vecteurs — la source de vérité reste le relay client validé) :

1. X-Relay-Client / X-Relay-Key présents → vérification RÉELLE en DB via la
   mécanique existante de app.main (_relay_auth_from_request, injectée par
   build_router pour éviter tout import circulaire), target "llm".
2. Sinon ``Authorization: Bearer <llmToken>`` → vérification de signature/exp
   (tokens.verify_llm_token) PUIS re-check de révocation du client en DB
   (un token signé encore valide ne doit pas survivre à une révocation).
3. Sinon 401 au format OpenAI.

Les accès DB (psycopg2, synchrone) sont exécutés hors event-loop via
anyio.to_thread — jamais d'appel bloquant dans les handlers async.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import anyio.to_thread
from fastapi import Request

from .errors import LlmProxyError
from .tokens import verify_llm_token

logger = logging.getLogger("device-management.llm")


@dataclass(frozen=True)
class LlmIdentity:
    client_uuid: str
    email: str
    auth_method: str  # "relay_headers" | "llm_token"
    expires_at: int | None = None


def _get_psycopg2():
    # Import paresseux via sys.modules à CHAQUE appel : les tests injectent un
    # faux psycopg2 dans sys.modules, et l'ordre d'import des modules ne doit
    # pas figer la résolution.
    try:
        import psycopg2  # noqa: PLC0415
        return psycopg2
    except ModuleNotFoundError:  # pragma: no cover
        return None


def _client_uuid_is_active(client_uuid: str) -> bool:
    """Re-check de révocation : le client_uuid a-t-il encore un credential actif ?

    Sans DB (dev/tests), on retombe sur le store mémoire de app.main ; store
    vide (pod neuf sans DB) → on accepte, la signature + l'exp du token font foi.
    """
    if not client_uuid:
        return False
    psycopg2 = _get_psycopg2()
    from ..services.db import db_url_bootstrap  # noqa: PLC0415
    db_url = db_url_bootstrap()
    if psycopg2 is not None and db_url:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM relay_clients
                    WHERE client_uuid = %s AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                    LIMIT 1
                    """,
                    (client_uuid,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()

    # Fallback mémoire (parité avec _verify_relay_credentials sans DB).
    from .. import main as _main  # noqa: PLC0415
    store = getattr(_main, "_RELAY_MEMORY_STORE", {}) or {}
    if not store:
        return True
    for row in store.values():
        if str(row.get("client_uuid") or "") == client_uuid and not row.get("revoked"):
            return True
    return False


class LlmAuthenticator:
    """Résout l'identité d'une requête LLM. relay_auth = app.main._relay_auth_from_request."""

    def __init__(self, relay_auth: Callable[..., tuple[bool, dict | str]]):
        self._relay_auth = relay_auth

    async def resolve(self, request: Request) -> LlmIdentity:
        relay_client = (
            request.headers.get("x-relay-client") or request.headers.get("x-client-id") or ""
        ).strip()
        if relay_client:
            ok, meta = await anyio.to_thread.run_sync(
                lambda: self._relay_auth(request, target="llm")
            )
            if not ok:
                raise LlmProxyError(401, f"Relay authentication failed: {meta}.",
                                    code="invalid_api_key")
            assert isinstance(meta, dict)
            return LlmIdentity(
                client_uuid=str(meta.get("client_uuid") or ""),
                email=str(meta.get("email") or ""),
                auth_method="relay_headers",
                expires_at=int(meta.get("expires_at") or 0) or None,
            )

        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                payload = verify_llm_token(token)  # lève LlmProxyError si invalide
                client_uuid = str(payload.get("client_uuid") or "")
                active = await anyio.to_thread.run_sync(_client_uuid_is_active, client_uuid)
                if not active:
                    raise LlmProxyError(401, "Relay client revoked or expired.",
                                        code="invalid_api_key")
                return LlmIdentity(
                    client_uuid=client_uuid,
                    email=str(payload.get("email") or ""),
                    auth_method="llm_token",
                    expires_at=int(payload.get("exp") or 0) or None,
                )

        raise LlmProxyError(
            401,
            "Missing credentials: provide X-Relay-Client/X-Relay-Key headers or "
            "Authorization: Bearer <llmToken> (obtained from /config after enrollment).",
            code="invalid_api_key",
        )
