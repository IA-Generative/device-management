# Leçons du banc

## L1 — Un composant sans clé stockait un paquet non signé sans rien dire
Constat : `POST /api/plugins/{slug}/deploy` envoyé au pod de service (sans clé) répondait 201,
`signed: false`. Le paquet partait sans enveloppe ni ancre, sans avertissement.
Correction : `DM_PACKAGE_SIGNING_REQUIRED` posé sur tous les composants ; un composant qui ne peut
pas signer refuse l'import (422 `signing_config`).
Conséquence d'exploitation : un envoi automatisé doit viser le composant d'administration.

## L0 — Banc : volumes nommés en root
L'image tourne en uid 10001 ; un volume nommé neuf appartient à root. Service `droits` ajouté.

## L2 — Réimporter le même paquet donnait un autre binaire
Constat : le second import d'un paquet identique était refusé (« ce numéro existe déjà avec un
binaire différent »). Le fichier d'ancre ajouté à l'archive portait l'heure de l'import.
Correction : entrée ajoutée avec une date fixe ; l'import est redevenu reproductible.

## L3 — Une fonction devenue asynchrone par erreur ne faisait plus rien, sans erreur
Constat : l'aperçu de l'envoi répondait 500, et le relevé de l'adaptation de configuration n'était
jamais joint côté administration (appel sans `await`, donc jamais exécuté). Le scénario HTTP ne le
voyait pas : il passait par l'API, pas par l'écran.
Correction : fonctions rendues synchrones ; le banc joue désormais les écrans dans un navigateur.
Règle retenue : tout chemin d'envoi a son contrôle au banc, pas seulement le plus commode.

## L4 — Après un envoi, la page se rechargeait sur place et perdait le message de retour
Constat (navigateur) : tous les formulaires de l'administration sont envoyés par `fetch` avec
`redirect: 'manual'`. La redirection est alors opaque, son adresse illisible, et le script
rechargeait la page courante. Le bandeau « liste de DM réécrite » de l'issue #133 et l'arrivée sur
l'onglet Déploiements ne s'affichaient donc jamais ; la confirmation d'invitation non plus.
Défaut antérieur à ce chantier, invisible sans navigateur.
Correction : `redirect: 'follow'`, puis navigation vers `resp.url`.

## L5 — La configuration embarquée n'était jamais enregistrée par l'écran d'envoi
Constat (test d'intégration) : par « Nouvelle version », le gabarit `dm-config.json` du paquet
était passé à la base sous forme de dictionnaire ; l'écriture échouait, l'erreur n'était que
journalisée. Seul l'envoi par l'API l'enregistrait. Défaut antérieur à ce chantier.
Correction : sérialisation avant écriture, et un test qui relit la base après l'envoi.

## L6 — Sur un lien d'essai, le premier bouton téléchargeait la version générale
Constat (capture d'écran du banc) : la page d'un essai gardait en tête le bouton « Télécharger
v0.25.0 ». Un testeur pressé repartait avec la version générale.
Correction : sur un lien d'essai, le bouton principal sert l'essai ; la version générale passe en
bouton secondaire.

## L7 — Le nom au catalogue n'est pas le nom que le poste connaît
Constat (relecture croisée avec le plugin) : le DM signait le nom du plugin au catalogue
(`mirai-matisse`), le poste se connaît sous un alias (`matisse`). Toute mise à jour signée aurait
été refusée.
Correction : la déclaration porte l'identifiant de l'extension lu dans le paquet ; c'est lui que le
plugin compare. À l'import, le nom signé par la CI est accepté s'il est un alias du plugin.
