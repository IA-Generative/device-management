"""Erreurs du relais d'outils : une forme unique {"error": {"code", "message"}}.

Codes du contrat (issue #116) : unauthorized, forbidden, not_found,
invalid_arguments, rate_limited (avec Retry-After), unavailable, blocked (refus de
l'amont : politique, contenu), upstream_error (l'outil amont a échoué : erreur d'outil
MCP isError ou 422 du service, sans lien avec un refus d'accès ni avec les arguments) ;
truncated n'est pas une erreur (champ de la réponse 200).
"""
from __future__ import annotations

from fastapi.responses import JSONResponse

CODES_PAR_STATUT = {
    400: "invalid_arguments",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    413: "invalid_arguments",
    422: "upstream_error",
    429: "rate_limited",
    502: "unavailable",
    503: "unavailable",
    504: "unavailable",
}


def tools_error(status_code: int, code: str, message: str, *, retry_after: int | None = None) -> JSONResponse:
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = str(max(0, int(retry_after)))
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers or None,
    )


class ToolsError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def to_response(self) -> JSONResponse:
        return tools_error(self.status_code, self.code, self.message, retry_after=self.retry_after)


def code_amont(status: int) -> str:
    """Ramène un statut HTTP amont à un code du contrat."""
    if status in CODES_PAR_STATUT:
        return CODES_PAR_STATUT[status]
    if 400 <= status < 500:
        return "invalid_arguments"
    return "unavailable"
