"""Backend registry — URL, clé et mapping modèle→backend viennent de la CONFIG.

Backend par défaut : LLM_BASE_URL + LLM_API_TOKEN (variables existantes du repo,
hot-reloadables depuis l'onglet Config admin ; alias du ticket :
LLM_BACKEND_URL / LLM_BACKEND_API_KEY). Multi-backends optionnel via la clé
LLM_BACKENDS (JSON) :

    {
      "backends": {
        "mistral": {"base_url": "https://…/v1", "token_env": "LLM_API_TOKEN_MISTRAL"}
      },
      "model_map": {"mistral-*": "mistral", "*": "default"}
    }

``token_env`` est une INDIRECTION (nom de variable d'environnement) : aucun
secret ne transite dans le JSON ni dans l'UI admin. Le registry est reconstruit
à chaque requête (lecture cfg() triviale) → ajout/bascule/failover de backend
sans redéploiement ni changement de code. Principe directeur (ADR) : le DM
évolue, le backend LLM reste un fournisseur d'inférence banalisé.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import dataclass

from .. import runtime_config

logger = logging.getLogger("device-management.llm")

DEFAULT_BACKEND_NAME = "default"


def public_llm_proxy_url() -> str:
    """URL publique du proxy annoncée aux plugins (llmEndpoint).

    PUBLIC_LLM_PROXY_URL si définie, sinon dérivée de PUBLIC_BASE_URL + /llm/v1
    (même pattern que relayAssistantBaseUrl). Hot-reloadable.
    """
    url = str(runtime_config.cfg("PUBLIC_LLM_PROXY_URL", "") or "").strip().rstrip("/")
    if url:
        return url
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return f"{public_base}/llm/v1" if public_base else ""


@dataclass(frozen=True)
class Backend:
    name: str
    base_url: str
    api_token: str


class BackendRegistry:
    def __init__(self, backends: dict[str, Backend], model_map: dict[str, str]):
        self.backends = backends
        self.model_map = model_map

    @classmethod
    def from_env(cls) -> BackendRegistry:
        backends: dict[str, Backend] = {}
        # Backend par défaut : variables existantes (alias ticket acceptés).
        base_url = str(
            runtime_config.cfg("LLM_BASE_URL", "")
            or runtime_config.cfg("LLM_BACKEND_URL", "")
            or ""
        ).strip().rstrip("/")
        api_token = str(
            runtime_config.cfg("LLM_API_TOKEN", "")
            or runtime_config.cfg("LLM_BACKEND_API_KEY", "")
            or ""
        ).strip()
        if base_url:
            backends[DEFAULT_BACKEND_NAME] = Backend(DEFAULT_BACKEND_NAME, base_url, api_token)

        model_map: dict[str, str] = {}
        raw = str(runtime_config.cfg("LLM_BACKENDS", "") or "").strip()
        if raw:
            try:
                spec = json.loads(raw)
                for name, entry in (spec.get("backends") or {}).items():
                    entry_url = str(entry.get("base_url") or "").strip().rstrip("/")
                    if not entry_url:
                        continue
                    token_env = str(entry.get("token_env") or "").strip()
                    entry_token = os.getenv(token_env, "") if token_env else ""
                    backends[str(name)] = Backend(str(name), entry_url, entry_token)
                model_map = {str(k): str(v) for k, v in (spec.get("model_map") or {}).items()}
            except Exception:
                logger.warning("LLM_BACKENDS: JSON invalide, registry par défaut seul")

        return cls(backends, model_map)

    def resolve(self, model: str) -> Backend | None:
        """Backend pour un modèle : fnmatch sur model_map (ordre déclaré), sinon default."""
        model = (model or "").strip()
        for pattern, backend_name in self.model_map.items():
            if fnmatch.fnmatch(model, pattern):
                backend = self.backends.get(backend_name)
                if backend:
                    return backend
        return self.backends.get(DEFAULT_BACKEND_NAME)
