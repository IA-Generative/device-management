"""Mapping span → fonction pour l'export parc (allowlist FIGÉE).

Le contrat de sortie (bus de la bêta) impose un CATALOGUE FERMÉ de fonctions
par plugin : une fonction hors liste n'est JAMAIS exportée. Ce module est la
source unique de vérité côté DM :

- ``FONCTIONS`` : le catalogue fermé, tel que fixé par le contrat ;
- ``SPAN_VERS_FONCTION`` : l'allowlist span → fonction, par plugin. La clé d'un
  événement est l'attribut ``plugin.action`` s'il est présent, sinon le
  ``span_name`` (cf. :func:`cle_evenement`).

Tout span non mappé est journalisé en debug UNE fois par nom, puis ignoré —
jamais exporté.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("dm-parc-mapping")

# Sentinelles : jamais comptées comme « poste » (télémétrie anonyme / défaut).
CLIENT_UUID_SENTINELLES = ("00000000-0000-0000-0000-000000000000", "telemetry-open")

# Plugins exportés : nom d'export → slug DM canonique ATTENDU. Le mapping des
# noms est CÔTÉ DM, le bus ne connaît que les noms d'export.
#
# Le slug de droite est une ATTENTE, pas une vérité : un site peut porter le
# plugin sous un autre slug. _resoudre_plugin() (parc_export.py) essaie donc les
# DEUX clés — le nom d'export et ce slug — chacune par slug ou par alias de
# catalogue. Relevé du DGX le 2026-09-05 : le catalogue y porte « matisse » comme
# slug et « mirai-matisse » n'existe nulle part ; chercher le seul slug canonique
# y perdait 6 versions et 90 installations sur 120, en silence.
#
# Ne PAS ajouter ici les variantes d'un site : c'est un contrat partagé, et la
# résolution s'en charge.
PLUGINS_EXPORTES: dict[str, str] = {
    "libreoffice": "mirai-libreoffice",
    "matisse": "mirai-matisse",
}

# Catalogue FERMÉ des fonctions par plugin (contrat parc.delta.v1 /
# parc.instantane.v1). Ne jamais exporter autre chose.
FONCTIONS: dict[str, frozenset[str]] = {
    "libreoffice": frozenset({
        "assistant.ouvert", "texte.resume", "texte.reformule", "texte.corrige",
        "texte.traduit", "texte.genere", "conversation.envoyee",
        "resultat.insere", "resultat.copie", "modele.choisi", "compte.lie",
        "aide.ouverte",
    }),
    "matisse": frozenset({
        "assistant.ouvert", "courriel.resume", "courriel.reformule",
        "courriel.traduit", "reponse.generee", "fil.resume", "resultat.insere",
        "modele.choisi", "compte.lie", "aide.ouverte",
    }),
}

# Allowlist span → fonction, par plugin. Mapping initial CONSERVATEUR : on ne
# mappe que les spans dont la sémantique est certaine ; le reste attendra
# d'être observé (log debug, jamais exporté).
SPAN_VERS_FONCTION: dict[str, dict[str, str]] = {
    "libreoffice": {
        # Ouverture du panneau assistant (sidebar) — un « assistant.ouvert » par ouverture.
        "AssistantOpen": "assistant.ouvert",
        # Résumé de la sélection (menu contextuel Writer).
        "SummarizeSelection": "texte.resume",
        # Trois gestes de réécriture regroupés sous texte.reformule :
        # simplification, extension (« générer la suite ») et modification guidée.
        "SimplifySelection": "texte.reformule",
        "ExtendSelection": "texte.reformule",
        "EditSelection": "texte.reformule",
        # Correction orthographique/grammaticale de la sélection.
        "CorrectSelection": "texte.corrige",
        # Traduction de la sélection.
        "TranslateSelection": "texte.traduit",
        # Un tour de conversation avec l'assistant (chaque AssistantRun = un envoi).
        "AssistantRun": "conversation.envoyee",
    },
    # TODO matisse : aucun span connu à ce jour (le plugin Thunderbird n'émet
    # pas encore de télémétrie mappable) — dict volontairement vide ; à remplir
    # dès les premiers spans observés, fonction par fonction.
    "matisse": {},
}

# Erreurs attribuables : (plugin, span, attribut, valeur-fausse) → la fonction
# porte l'erreur. Seul cas certain aujourd'hui : AssistantRun avec
# attributes.assistant.ok = false → une erreur de conversation.envoyee.
# PLANCHER ASSUMÉ : AssistantToolCall avec tool.ok=false et LlmRelayError ne
# sont PAS attribuables à une fonction précise (un tour peut enchaîner
# plusieurs outils, le relay ne dit pas quel geste a échoué) → non exportés ;
# le compteur `erreurs` est donc un plancher, jamais un plafond.
_ATTRIBUT_ERREUR: dict[tuple[str, str], str] = {
    ("libreoffice", "AssistantRun"): "assistant.ok",
}

# Spans inconnus déjà journalisés (une seule ligne de log par nom de span).
_spans_inconnus_vus: set[str] = set()


def cle_evenement(span_name: str, attributes: dict | None) -> str:
    """Clé de mapping d'un événement : ``plugin.action`` si présent, sinon span_name."""
    if isinstance(attributes, dict):
        action = attributes.get("plugin.action")
        if action:
            return str(action)
    return str(span_name or "")


def _est_faux(valeur) -> bool:
    """Booléen OTLP tolérant : False natif ou chaîne 'false' (défensif)."""
    if valeur is False:
        return True
    return isinstance(valeur, str) and valeur.strip().lower() == "false"


def mapper_evenement(span_name: str, attributes: dict | None) -> tuple[str, str, bool] | None:
    """Mappe un événement de télémétrie vers (plugin, fonction, est_erreur).

    Retourne None si le span n'est pas dans l'allowlist (journalisé en debug
    une fois par nom, jamais exporté).
    """
    cle = cle_evenement(span_name, attributes)
    if not cle:
        return None
    for plugin, table in SPAN_VERS_FONCTION.items():
        fonction = table.get(cle)
        if fonction is None:
            continue
        est_erreur = False
        attr_erreur = _ATTRIBUT_ERREUR.get((plugin, cle))
        if attr_erreur and isinstance(attributes, dict) and _est_faux(attributes.get(attr_erreur)):
            est_erreur = True
        return plugin, fonction, est_erreur
    if cle not in _spans_inconnus_vus:
        _spans_inconnus_vus.add(cle)
        logger.debug("parc export: span non mappé, jamais exporté: %s", cle[:120])
    return None


def cles_pour(plugin: str, fonction: str) -> list[str]:
    """Mapping inverse : les clés (span/plugin.action) qui alimentent une fonction.

    Sert au recalcul SQL de `postes` (COUNT DISTINCT client_uuid sur la fenêtre
    du jour) pour une combinaison touchée.
    """
    return sorted(
        cle for cle, f in SPAN_VERS_FONCTION.get(plugin, {}).items() if f == fonction
    )
