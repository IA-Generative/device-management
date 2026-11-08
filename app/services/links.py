"""Liens distribués aux applications par ``/config`` (clé ``links``).

Contrat (issue #83) : une liste PLATE et RÉSOLUE côté serveur, remplacée en bloc par
le client à chaque ``/config`` — jamais fusionnée côté poste, pour les mêmes raisons
que ``features`` (zéro lien fantôme). Chaque entrée nomme le RÔLE du lien dans
l'interface (``id``), pas sa destination : la destination change, le rôle non.

Entrée : ``{id, label, url, hint?, hintUrl?}`` ; ``hidden: true`` dans une couche
retire le lien pour cette couche et les suivantes.

Couches, de la moins à la plus prioritaire (fusion PAR ``id``, champ par champ) :

1. ``DM_LINKS``            — liste commune à toutes les applications (values du chart,
                             surchargeable à chaud dans l'onglet Config de l'admin) ;
2. ``DM_LINKS_BY_APP``     — objet ``{device_type: [...]}``, entrées propres à une
                             application ;
3. gabarit ``dm-config.json`` du plugin — ``links`` de ``default`` puis du profil ;
4. surcharge catalogue (``plugin_env_overrides``, clé ``links``, JSON).

Une entrée hors contrat est ignorée individuellement (journalisée), jamais la liste.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("device-management.links")

LINKS_KEY = "links"
ENV_COMMON = "DM_LINKS"
ENV_BY_APP = "DM_LINKS_BY_APP"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_LABEL = 80
MAX_HINT = 200
MAX_URL = 2048
_URL_FIELDS = ("url", "hintUrl")
_TEXT_LIMITS = {"label": MAX_LABEL, "hint": MAX_HINT}
_FIELDS = ("id", "label", "url", "hint", "hintUrl", "hidden")
_TEMPLATE_VAR_RE = re.compile(r"\$\{\{?[A-Za-z_][A-Za-z0-9_]*\}?\}")


def _valid_url(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("https://")
        and len(value) <= MAX_URL
        and not _TEMPLATE_VAR_RE.search(value)
        and not any(c.isspace() for c in value)
    )


def normalize_entry(raw: Any, *, source: str = "") -> dict | None:
    """Validate one (possibly PARTIAL) entry. Returns a dict with only the fields
    present, or None when the entry is unusable (bad id, bad url…).

    A partial entry (``{"id": "x", "url": "https://…"}``) is accepted here: whether
    it is complete is decided after the merge (see :func:`finalize`).
    """
    if not isinstance(raw, dict):
        logger.warning("links[%s]: entrée ignorée (pas un objet) : %r", source, raw)
        return None
    link_id = raw.get("id")
    if not isinstance(link_id, str) or not ID_RE.match(link_id):
        logger.warning("links[%s]: entrée ignorée (id invalide) : %r", source, link_id)
        return None
    out: dict[str, Any] = {"id": link_id}
    for field in _URL_FIELDS:
        if field in raw:
            value = raw[field]
            if value in (None, ""):
                out[field] = ""  # vider explicitement (ex. retirer hintUrl)
            elif _valid_url(value):
                out[field] = value.strip()
            else:
                logger.warning("links[%s] %s: %s ignoré (https:// requis, ≤ %d car., sans ${{VAR}})",
                               source, link_id, field, MAX_URL)
                if field == "url":
                    return None
    for field, limit in _TEXT_LIMITS.items():
        if field in raw:
            value = raw[field]
            if value is None:
                value = ""
            if not isinstance(value, str):
                logger.warning("links[%s] %s: %s ignoré (chaîne attendue)", source, link_id, field)
                continue
            value = " ".join(value.split())
            if len(value) > limit:
                logger.warning("links[%s] %s: %s tronqué à %d car.", source, link_id, field, limit)
                value = value[:limit].rstrip()
            out[field] = value
    if "hidden" in raw:
        out["hidden"] = bool(raw["hidden"])
    return out


def parse_list(raw: Any, *, source: str = "") -> list[dict]:
    """Parse a layer: a list, or its JSON text. Anything else → []."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except ValueError:
            logger.warning("links[%s]: JSON invalide, couche ignorée", source)
            return []
    if not isinstance(raw, list):
        logger.warning("links[%s]: liste attendue, couche ignorée", source)
        return []
    out = []
    for item in raw:
        entry = normalize_entry(item, source=source)
        if entry is not None:
            out.append(entry)
    return out


def parse_by_app(raw: Any, device_type: str | None) -> list[dict]:
    """Layer 2: ``{device_type: [...]}`` (object or JSON text) → the list for this app."""
    if not device_type or raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except ValueError:
            logger.warning("links[%s]: JSON invalide, couche ignorée", ENV_BY_APP)
            return []
    if not isinstance(raw, dict):
        logger.warning("links[%s]: objet {device_type: [...]} attendu, couche ignorée", ENV_BY_APP)
        return []
    return parse_list(raw.get(device_type), source=f"{ENV_BY_APP}.{device_type}")


def merge(*layers: list[dict]) -> list[dict]:
    """Merge layers by id, field by field; later layers win. Order = first
    appearance of each id (common list first, then app-specific additions)."""
    merged: dict[str, dict] = {}
    for layer in layers:
        for entry in layer:
            current = merged.setdefault(entry["id"], {"id": entry["id"]})
            current.update(entry)
    return list(merged.values())


def finalize(entries: list[dict]) -> list[dict]:
    """Drop hidden or incomplete entries, emit the public shape."""
    out = []
    for entry in entries:
        if entry.get("hidden"):
            continue
        label = entry.get("label", "")
        url = entry.get("url", "")
        if not label or not url:
            logger.warning("links: %s incomplet (label et url requis), non servi", entry["id"])
            continue
        public = {"id": entry["id"], "label": label, "url": url}
        if entry.get("hint"):
            public["hint"] = entry["hint"]
        if entry.get("hintUrl"):
            public["hintUrl"] = entry["hintUrl"]
        out.append(public)
    return out


def resolve(*layers: Any, device_type: str | None = None, by_app: Any = None) -> list[dict]:
    """Full resolution: ``layers`` are lists (or JSON text) in increasing priority;
    ``by_app`` (layer 2) is inserted right after the first layer."""
    parsed = [parse_list(layer, source=f"layer{i}") for i, layer in enumerate(layers)]
    if by_app is not None:
        parsed.insert(1, parse_by_app(by_app, device_type))
    return finalize(merge(*parsed))
