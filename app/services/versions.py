"""Comparaison de versions de plugin et de plateforme.

Module neutre : importable à la fois par app.main et par app.admin.services
sans cycle d'import.
"""

from __future__ import annotations

import re


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


# ── Comparaison avec pré-versions (issue #132) ───────────────────────────────
# parse_version_tuple réduit « 0.24.0-rc.179 » à (0,) : une campagne ordinaire
# qui visait une rc ne servait donc jamais personne (installée < cible toujours
# faux). La directive de mise à jour compare désormais avec update_sort_key.
# Le gating des flags garde parse_version_tuple et son repli (0,).

_PRE_TOKEN_RE = re.compile(r"\d+|\D+")
_FALLBACK_KEY = ((0,), 1, ())


def version_sort_key(v) -> tuple | None:
    """Clé de tri d'une version, pré-version comprise ; None si illisible.

    Règles (celles de semver, plus tolérantes sur le suffixe) :
      - le cœur numérique se compare comme parse_version_tuple ;
      - à cœur égal, une pré-version précède la version finale
        (« 1.2.0-rc.3 » < « 1.2.0 ») ;
      - les identifiants de pré-version se comparent un à un, la partie
        numérique en nombre (« rc.9 » < « rc.10 », « rc9 » < « rc10 ») ;
      - les métadonnées de build (« +abc ») sont ignorées.
    """
    text = str(v or "").strip().split("+", 1)[0]
    if not text:
        return None
    core_text, sep, pre_text = text.partition("-")
    core = try_parse_version_tuple(core_text)
    if core is None:
        return None
    if not sep:
        return (core, 1, ())
    if not pre_text:
        return None
    tokens = []
    for ident in pre_text.split("."):
        if not ident:
            return None
        for tok in _PRE_TOKEN_RE.findall(ident):
            # (0, n, "") numérique < (1, 0, "txt") alphabétique, comme semver.
            tokens.append((0, int(tok), "") if tok.isdigit() else (1, 0, tok.lower()))
        # Séparateur d'identifiant : « rc » < « rc.1 » (moins d'identifiants = avant).
        tokens.append((-1, 0, ""))
    return (core, 0, tuple(tokens))


def update_sort_key(v) -> tuple:
    """version_sort_key avec le repli historique : illisible → équivalent de (0,)."""
    return version_sort_key(v) or _FALLBACK_KEY


UNORDERABLE_VERSION_HELP = (
    "Forme de version non reconnue : « {version} ». Attendu des nombres séparés par des points, "
    "éventuellement suivis d'un suffixe (1.2.3, 1.2.3-rc.4). Une campagne ordinaire ne servirait "
    "cette version à aucun poste. Corrigez le numéro, ou publiez-la comme expérimentale et "
    "servez-la par une campagne d'expérimentation."
)


def unorderable_version_message(v) -> str | None:
    """Message à afficher si `v` ne peut pas être ordonnée, None sinon.

    Une version illisible garde le repli (0,) : aucune campagne ordinaire ne la
    sert. Les chemins qui créent une telle campagne la refusent avec ce message
    (issue #132) au lieu de créer une campagne qui ne sert personne.
    """
    if version_sort_key(v) is not None:
        return None
    return UNORDERABLE_VERSION_HELP.format(version=str(v or "").strip())
