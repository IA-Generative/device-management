"""Relais d'outils (/tools/v1) : le DM relaie les appels d'outils du plugin vers
les services MirAI (REST et MCP), avec l'identité de la personne.

Même modèle que le proxy LLM (app.llm) : registre par configuration, auth du
poste par les en-têtes relay, bornes, journal sans contenu. Voir
docs/operations/relais-outils.md et l'issue #116.
"""
