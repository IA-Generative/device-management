"""Validation des arguments d'un outil contre son schéma JSON (jsonschema, Draft 2020-12).

Le relais valide AVANT d'appeler l'amont : une erreur devient `invalid_arguments`
sans appel réseau. Le message ne cite que le chemin et la règle, jamais la valeur.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

_validateurs: dict[str, Draft202012Validator] = {}


def _validateur(schema: dict) -> Draft202012Validator | None:
    import json
    cle = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    v = _validateurs.get(cle)
    if v is None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            return None
        v = Draft202012Validator(schema)
        if len(_validateurs) > 256:
            _validateurs.clear()
        _validateurs[cle] = v
    return v


def erreurs_arguments(schema: dict, arguments: dict) -> list[str]:
    """Liste des violations, sous forme « chemin : règle » sans la valeur fournie."""
    v = _validateur(schema)
    if v is None:
        return ["schéma de l'outil invalide"]
    erreurs = []
    for e in sorted(v.iter_errors(arguments), key=lambda x: list(x.absolute_path)):
        chemin = ".".join(str(p) for p in e.absolute_path) or "(racine)"
        regle = e.validator
        if regle in ("required", "additionalProperties"):
            erreurs.append(f"{chemin} : {regle} ({e.message.split(' is a required property')[0]})"
                           if regle == "required" else f"{chemin} : {regle}")
        else:
            erreurs.append(f"{chemin} : {regle}")
    return erreurs
