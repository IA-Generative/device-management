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


# ── Nature d'une version (issue #106) ────────────────────────────────────────
NATURE_GENERAL = "general"      # X.Y.Z
NATURE_CANDIDATE = "rc"         # X.Y.Z-rc.N
NATURE_TRIAL = "dev"            # X.Y.Z-dev.N
NATURE_OTHER = "other"          # autre suffixe, ou forme non reconnue

_NATURE_LABELS = {
    NATURE_GENERAL: "Générale",
    NATURE_CANDIDATE: "Candidate",
    NATURE_TRIAL: "Essai",
    NATURE_OTHER: "Pré-version",
}


def version_nature(v) -> str:
    """Nature d'une version d'après son numéro : générale, candidate, essai, autre.

    Convention : « X.Y.Z » générale, « X.Y.Z-rc.N » candidate, « X.Y.Z-dev.N »
    essai. Tout autre suffixe est une pré-version sans nature précise.
    """
    text = str(v or "").strip().split("+", 1)[0]
    core, sep, pre = text.partition("-")
    if try_parse_version_tuple(core) is None:
        return NATURE_OTHER
    if not sep:
        return NATURE_GENERAL
    head = pre.split(".", 1)[0].lower()
    if re.fullmatch(r"rc\d*", head):
        return NATURE_CANDIDATE
    if re.fullmatch(r"dev\d*", head):
        return NATURE_TRIAL
    return NATURE_OTHER


def nature_label(nature: str) -> str:
    return _NATURE_LABELS.get(nature, _NATURE_LABELS[NATURE_OTHER])


def is_prerelease(v) -> bool:
    return version_nature(v) != NATURE_GENERAL


PRERELEASE_ORDINARY_CODE = "prerelease_ordinary"


def prerelease_campaign_message(v, app_env: str) -> str | None:
    """Message si `v` est une pré-version qu'une campagne ordinaire pousserait en production.

    Issue #106 : sur une instance de production, une candidate ou un essai se
    diffuse par une campagne d'expérimentation, sur une cohorte. Une campagne
    ordinaire la proposerait à tout poste en version antérieure : c'est
    l'exception, elle demande une confirmation et laisse une trace.
    """
    if str(app_env or "").strip().lower() not in ("prod", "production"):
        return None
    if not is_prerelease(v):
        return None
    return (f"« {str(v).strip()} » est une pré-version ({nature_label(version_nature(v)).lower()}). "
            "Sur cette instance de production, une campagne ordinaire la proposerait à tous les "
            "postes en version antérieure. Préférez une expérimentation sur une cohorte, ou "
            "confirmez ce déploiement.")


# ── Écriture numérique d'une pré-version pour LibreOffice ────────────────────
# LibreOffice accepte un suffixe dans <version value="…"> de description.xml,
# mais le range mal : il compare élément par élément, le plus long d'abord, si
# bien que « 0.4.0-rc.12 » passe pour plus récent que « 0.4.0 » et que « 0.4.1 ».
# Un poste en candidate ne se verrait jamais proposer la finale par le flux natif.
# Le paquet inscrit donc dans son manifeste une écriture purement numérique, qui
# se range correctement, et annonce au DM le numéro complet.
#
#   X.Y.Z        → X.Y.Z
#   X.Y.Z-dev.N  → P.1.N      P = le plus grand numéro à trois éléments avant X.Y.Z :
#   X.Y.Z-rc.N   → P.2.N      X.Y.(Z-1) ; X.(Y-1).99999 si Z = 0 ; (X-1).99999.99999 si Y = 0 aussi.
#
# Ordre obtenu : 0.3.1 < 0.4.0-dev.7 (0.3.99999.1.7) < 0.4.0-rc.12 (0.3.99999.2.12) < 0.4.0.
_LO_MAX = 99999
_LO_RANK = {NATURE_TRIAL: 1, NATURE_CANDIDATE: 2}
# La forme de la convention seulement (« rc.N », « dev.N ») : un libellé plus ancien
# comme « 1.3.0-rc1 » continue d'être annoncé tel quel, comme son paquet le porte.
_LO_PRE_RE = re.compile(r"(?:rc|dev)\.(\d+)", re.IGNORECASE)


def libreoffice_numeric_version(v) -> str | None:
    """Écriture numérique de `v` pour le manifeste et le flux natif de LibreOffice.

    None si `v` n'a pas la forme X.Y.Z, X.Y.Z-rc.N ou X.Y.Z-dev.N, ou si elle
    n'a pas de prédécesseur (0.0.0-rc.N).
    """
    text = str(v or "").strip().split("+", 1)[0]
    core_text, sep, pre = text.partition("-")
    core = try_parse_version_tuple(core_text)
    if core is None or len(core) != 3 or any(part < 0 or part >= _LO_MAX for part in core):
        return None
    if not sep:
        return core_text
    rank = _LO_RANK.get(version_nature(text))
    match = _LO_PRE_RE.fullmatch(pre)
    if rank is None or not match:
        return None
    x, y, z = core
    if z > 0:
        previous = (x, y, z - 1)
    elif y > 0:
        previous = (x, y - 1, _LO_MAX)
    elif x > 0:
        previous = (x - 1, _LO_MAX, _LO_MAX)
    else:
        return None
    return ".".join(str(part) for part in (*previous, rank, int(match.group(1))))
