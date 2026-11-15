# Banc d'essai — chaîne de confiance des paquets

Rejoue de bout en bout, sur une machine de développement, ce que décrit l'ADR-0006 : un
paquet neutre et sa signature d'origine arrivent, le DM vérifie, adapte, signe, et un poste
simulé vérifie avant d'installer. Puis les écrans sont joués dans un vrai navigateur.

La topologie est celle de la production, en petit : un composant `admin` qui importe et
détient la clé de signature, un composant `api` qui sert, sans clé et avec son propre
volume, une base PostgreSQL 18. Un seul conteneur masquerait les défauts qui tiennent à
cette séparation.

## Lancer

    uv pip install -p .venv/bin/python httpx playwright && .venv/bin/python -m playwright install chromium
    scripts/banc-paquets/banc.sh              # délégation par une racine (défaut)
    scripts/banc-paquets/banc.sh simple       # mode simple : la clé du DM est sa propre ancre

Prérequis : Docker, Node 20 ou plus, et l'environnement Python du dépôt. Ports 18090
(service) et 18091 (administration). Chaque lancement repart de zéro : clés neuves, image
reconstruite depuis l'arbre de travail, base vide. Les clés ne servent qu'au banc et ne
quittent pas `common.env` / `admin.env`, ignorés par Git.

Pour joindre le module de vérification du plugin Thunderbird lui-même :

    PLUGIN_VERIFIER=/chemin/iassistant-matisse/thunderbird/60.9.1/modules/package-signature.js \
      scripts/banc-paquets/banc.sh

Sans lui, ces contrôles sont sautés ; le vérificateur de référence `verify.mjs`, écrit avec
WebCrypto comme le fera un plugin, joue toujours.

## Ce qui est joué

| Fichier | Contenu |
|---|---|
| `gen.py` | clés jetables, fichiers d'environnement, paquets neutres (Thunderbird, LibreOffice) et leurs signatures d'origine, cas d'attaque |
| `run.py` | scénario HTTP : refus à l'import, version générale, directive et vérification côté poste, composant sans clé, cohabitation générale + candidate + deux essais, sortie de la finale, LibreOffice, import depuis les releases GitHub (faux serveur `scripts/faux_github.py`) |
| `ui.py` | scénario navigateur : fiche du plugin, aperçu de l'import, envoi d'un essai avec confirmation, invitation, catalogue public |
| `verify.mjs` | vérificateur de référence indépendant du code du DM |

Les captures d'écran vont dans `out/ecrans/`.

## Ce que le banc a appris

Voir [LECONS.md](LECONS.md). Chaque défaut trouvé ici a désormais son test dans `tests/`.
