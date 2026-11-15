"""Configuration embarquée dans un paquet (`dm-config.json`) : adaptation à l'instance.

Issue #107. Un paquet neutre ne porte, pour les réglages que le DM gouverne, que
des repères `${{VAR}}` ou rien. Un paquet construit pour un autre environnement
les porte en dur : déployé ici, il pointerait vers le mauvais Keycloak ou le
mauvais LLM. À l'import, les valeurs gouvernées sont donc remplacées par le
repère correspondant, que le DM résout avec ses propres valeurs quand il sert
la configuration.

Les autres clés (bascules de fonctions, prompts, réglages métier) appartiennent
au plugin : elles ne sont jamais touchées.

Module neutre : importable par app.main et app.admin sans cycle.
"""
from __future__ import annotations

import os
import re

# Clé gouvernée → repère résolu par le DM à la diffusion de la configuration.
GOVERNED = {
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}",
}
LOCAL_PROFILES = frozenset({"local"})
_NON_PROFILES = frozenset({"configVersion", "default"})
_PLACEHOLDER_RE = re.compile(r"\$\{\{([A-Z0-9_]+)\}\}|\$\{([A-Z0-9_]+)\}")
_SECRET_KEY_RE = re.compile(r"token|secret|password|apikey|api_key", re.IGNORECASE)

# États d'une clé gouvernée dans le relevé (stables : affichés, tracés, testés).
COMPLETED = "completed"        # absente ou vide dans le paquet : repère ajouté
PLACEHOLDER = "placeholder"    # déjà un repère : rien à faire
REPLACED = "replaced"          # valeur en dur remplacée par le repère
IDENTICAL = "identical"        # valeur en dur égale à celle du DM : remplacée, sans effet
KEPT_UNKNOWN = "kept_unknown"  # le DM n'a pas de valeur pour ce repère : valeur du paquet gardée
KEPT = "kept"                  # adaptation refusée par l'administrateur : valeur du paquet gardée


def _is_placeholder(value) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.search(value))


def _instance_value(placeholder: str) -> str:
    m = _PLACEHOLDER_RE.search(placeholder)
    name = (m.group(1) or m.group(2)) if m else ""
    return (os.environ.get(name) or "").strip()


def _shown(key: str, value) -> str:
    """Valeur affichable : jamais un secret, jamais plus d'une ligne."""
    if _SECRET_KEY_RE.search(key):
        return "(valeur masquée)"
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= 120 else text[:117] + "…"


def server_profiles(template: dict) -> list[str]:
    return [name for name, section in template.items()
            if name not in _NON_PROFILES and name not in LOCAL_PROFILES and isinstance(section, dict)]


def adapt(template: dict, *, replace: bool = True) -> tuple[dict, list[dict]]:
    """Adapte `template` à l'instance (en place) → (template, relevé).

    Pour chaque profil serveur et chaque clé gouvernée : une clé absente ou
    vide reçoit son repère (comportement historique) ; une valeur en dur est
    remplacée par le repère si `replace`, sauf quand le DM n'a aucune valeur à
    y mettre — la remplacer viderait le réglage. `replace=False` garde les
    valeurs du paquet et les signale.

    Relevé : [{profile, key, state, package_value}] pour les seules clés gouvernées.
    """
    report: list[dict] = []
    if not isinstance(template, dict):
        return template, report
    for profile in server_profiles(template):
        section = template[profile]
        for key, placeholder in GOVERNED.items():
            value = section.get(key)
            if value is None or value == "":
                section[key] = placeholder
                state = COMPLETED
            elif _is_placeholder(value):
                state = PLACEHOLDER
            else:
                known = _instance_value(placeholder)
                if not replace:
                    state = KEPT
                elif not known:
                    state = KEPT_UNKNOWN
                else:
                    state = IDENTICAL if str(value).strip() == known else REPLACED
                    section[key] = placeholder
            entry = {"profile": profile, "key": key, "state": state}
            if state in (REPLACED, IDENTICAL, KEPT, KEPT_UNKNOWN):
                entry["package_value"] = _shown(key, value)
            report.append(entry)
    return template, report


def summary(report: list[dict]) -> dict:
    """Ce que l'administrateur doit savoir du relevé, en une phrase par cas."""
    def of(state):
        return [e for e in report if e["state"] == state]

    def names(entries):
        return ", ".join(f"{e['profile']}.{e['key']}" for e in entries)

    replaced, unknown, kept = of(REPLACED), of(KEPT_UNKNOWN), of(KEPT)
    messages: list[str] = []
    if replaced:
        messages.append(f"{len(replaced)} valeur(s) du paquet remplacée(s) par celles de cette instance : "
                        f"{names(replaced)}.")
    if unknown:
        messages.append(f"{len(unknown)} valeur(s) du paquet gardée(s) faute de valeur côté instance : "
                        f"{names(unknown)}.")
    if kept:
        messages.append(f"{len(kept)} valeur(s) du paquet gardée(s) à votre demande, différentes de "
                        f"celles de cette instance : {names(kept)}.")
    return {
        "replaced": [f"{e['profile']}.{e['key']}" for e in replaced],
        "kept_unknown": [f"{e['profile']}.{e['key']}" for e in unknown],
        "kept": [f"{e['profile']}.{e['key']}" for e in kept],
        "messages": messages,
    }
