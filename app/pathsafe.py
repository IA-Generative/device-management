"""Jointure de chemin sûre et unifiée contre la traversée de répertoire.

Point d'entrée unique pour tout endpoint qui construit un chemin disque à partir
d'une valeur fournie par l'utilisateur (nom de fichier d'upload, segment de path,
slug, version, variante). Remplace les helpers historiques divergents
(``_safe_path_join`` côté API, ``_safe_resolve`` côté admin, vérifs
``normpath``+``startswith``) par une implémentation durcie :

- ``realpath`` résout les liens symboliques (les anciens helpers laissaient
  passer un symlink sortant) ;
- la frontière est testée sur le séparateur de chemin, donc ``/base`` n'autorise
  pas ``/base-evil``.
"""

from __future__ import annotations

import os
import re

from fastapi import HTTPException

# Segment sûr : un seul composant de chemin (ni "/", ni "..", ni nul).
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def safe_path_join(base_dir: str, relative_path: str) -> str:
    """Joint ``relative_path`` sous ``base_dir`` en bloquant la traversée.

    Retourne le chemin absolu résolu s'il reste à l'intérieur de ``base_dir``,
    sinon lève ``HTTPException(400)``.
    """
    base_abs = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base_abs, relative_path.lstrip("/")))
    if candidate == base_abs or candidate.startswith(base_abs + os.sep):
        return candidate
    raise HTTPException(status_code=400, detail="Invalid path")


def safe_segment(value: str, field: str = "name") -> str:
    """Valide un segment de chemin unique (nom de fichier, slug, version...).

    Réduit au ``basename`` puis impose la whitelist ``_SAFE_SEGMENT``. Lève
    ``HTTPException(400)`` si la valeur est vide ou contient un caractère
    interdit (``/``, ``..``, etc.).
    """
    value = os.path.basename(value or "")
    if not _SAFE_SEGMENT.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return value
