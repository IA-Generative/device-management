"""Règles de la directive de mise à jour — implémentation unique.

`/config` (app.main._build_update_directive) et l'administration (postes
éligibles d'une campagne, app.admin.services.campaigns.campaign_reach)
décident avec les mêmes fonctions, pour que le compte affiché à l'admin ne
dérive pas de ce que `/config` sert (issue #132).

Module neutre : importable par app.main et app.admin.services sans cycle.
"""

from __future__ import annotations

import datetime
import hashlib
import time

from .versions import update_sort_key

# Raisons renvoyées par decide_update (stables : affichées et testées).
UPDATE = "update"
ROLLBACK = "rollback"
NO_VERSION = "no_version"        # version installée inconnue du serveur
NO_TARGET = "no_target"          # campagne sans artefact / sans version
UP_TO_DATE = "up_to_date"        # déjà sur la version cible
AHEAD = "ahead"                  # version installée postérieure, pas de rollback

_UNKNOWN_VERSIONS = ("", "unknown", "0")


def decide_update(*, plugin_version: str, target_version: str,
                  is_experiment: bool = False, has_rollback: bool = False) -> tuple[str | None, str]:
    """(action, raison) pour un poste face à une campagne, hors cohorte et palier.

    action ∈ {"update", "rollback", None}. Campagne d'expérimentation : la cible
    est épinglée, servie dès que le poste n'y est pas (pas d'ordre exigé).
    Campagne ordinaire : servie si installée < cible, pré-versions comprises
    (« 0.24.0-rc.3 » < « 0.24.0 ») ; installée > cible → rollback s'il existe.
    """
    plugin_version = str(plugin_version or "").strip()
    target_version = str(target_version or "").strip()
    if plugin_version in _UNKNOWN_VERSIONS:
        return None, NO_VERSION
    if not target_version:
        return None, NO_TARGET
    if plugin_version == target_version:
        return None, UP_TO_DATE
    if is_experiment:
        return UPDATE, UPDATE
    installed, target = update_sort_key(plugin_version), update_sort_key(target_version)
    if installed < target:
        return UPDATE, UPDATE
    if installed == target:
        # Même version à l'écriture près (« 1.2.0 » / « 1.2.0+build ») : rien à servir.
        return None, UP_TO_DATE
    if has_rollback:
        return ROLLBACK, ROLLBACK
    return None, AHEAD


def current_rollout_percent(started_at, stages: list) -> int:
    """Pourcentage du palier en cours, d'après le temps écoulé depuis `started_at`."""
    if started_at is None:
        return 100
    if hasattr(started_at, "timestamp"):
        start_ts = started_at.timestamp()
    else:
        try:
            start_ts = datetime.datetime.fromisoformat(str(started_at)).timestamp()
        except Exception:
            return 100
    elapsed_hours = (time.time() - start_ts) / 3600
    cumulative_hours = 0
    for stage in stages:
        cumulative_hours += stage.get("duration_hours", 0)
        if elapsed_hours < cumulative_hours or stage.get("percent", 100) == 100:
            return stage.get("percent", 100)
    return 100


def campaign_rollout_percent(rollout_config, started_at) -> int:
    """Palier courant d'une campagne ; 100 sans déploiement progressif."""
    if not isinstance(rollout_config, dict):
        return 100
    stages = rollout_config.get("stages") or []
    if not stages:
        return 100
    return current_rollout_percent(started_at, stages)


def rollout_admits(client_uuid: str, percent: int) -> bool:
    """Le poste est-il dans le palier `percent` ? Sans UUID, pas de tirage : admis."""
    if percent >= 100 or not client_uuid:
        return True
    bucket = int(hashlib.md5(client_uuid.encode(), usedforsecurity=False).hexdigest()[:8], 16) % 100
    return bucket < percent


def percentage_cohort_admits(client_uuid: str, percentage: int) -> bool:
    """Appartenance à une cohorte de type `percentage` (tirage stable par UUID)."""
    if percentage <= 0 or not client_uuid:
        return False
    return int(hashlib.sha256(client_uuid.encode()).hexdigest(), 16) % 100 < percentage
