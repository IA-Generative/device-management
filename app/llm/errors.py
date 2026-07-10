"""Erreurs du proxy LLM au format objet OpenAI, exploitables par un client figé.

Le plugin ne sait afficher qu'un message générique si l'erreur n'est pas un
statut HTTP propre : on renvoie donc toujours un vrai code + un corps JSON
{"error": {"message", "type", "code"}} (+ "retry_after" top-level pour le 429,
exigence du contrat) — jamais d'échec silencieux.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    status_code: int,
    message: str,
    *,
    err_type: str = "invalid_request_error",
    code: str | None = None,
    retry_after: int | None = None,
) -> JSONResponse:
    body: dict = {"error": {"message": message, "type": err_type, "code": code}}
    headers: dict[str, str] = {}
    if retry_after is not None:
        body["retry_after"] = int(retry_after)
        headers["Retry-After"] = str(int(retry_after))
    return JSONResponse(status_code=status_code, content=body, headers=headers or None)


class LlmProxyError(Exception):
    """Erreur métier du proxy, convertible en réponse OpenAI-like."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        err_type: str = "invalid_request_error",
        code: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.err_type = err_type
        self.code = code
        self.retry_after = retry_after

    def to_response(self) -> JSONResponse:
        return openai_error(
            self.status_code,
            self.message,
            err_type=self.err_type,
            code=self.code,
            retry_after=self.retry_after,
        )
