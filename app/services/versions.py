"""Comparaison de versions de plugin et de plateforme.

Module neutre : importable à la fois par app.main et par app.admin.services
sans cycle d'import.
"""

from __future__ import annotations


def try_parse_version_tuple(v) -> tuple | None:
    """Version -> tuple d'entiers comparable, ou None si non parsable
    (vide, suffixe pré-release « 1.6.0-rc1 », segment non numérique)."""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return None


def parse_version_tuple(v) -> tuple:
    """Parse a version string into a tuple of ints for comparison.

    Supports any number of segments (semver 3, or extended 4-5 segments).
    Non parsable -> (0,) : le gating des flags et des campagnes s'appuie sur
    ce repli.
    """
    return try_parse_version_tuple(v) or (0,)
