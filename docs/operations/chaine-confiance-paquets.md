# Chaîne de confiance des paquets — mode opératoire

Ce guide dit comment mettre en service, sur une instance, ce que décrit
l'[ADR-0006](../architecture/adr-0006-chaine-confiance-paquets.md) : vérifier d'où vient un
paquet, l'adapter à l'instance, le signer, et laisser le plugin vérifier avant d'installer.

Sans aucun réglage, rien ne change : l'import et la directive de mise à jour sont ceux d'avant.

## Ce que fait l'import, dans l'ordre

Tous les chemins d'envoi passent par le même point (`app/services/package_import.py`) :
« Nouvelle version » de l'administration, ancien formulaire de déploiement, création d'un
plugin avec son premier paquet, `POST /api/plugins/{slug}/deploy`, `POST /api/artifacts`.

1. **Relevé** du paquet reçu : empreinte, numéros de version qu'il déclare, nature.
2. **Concordance des numéros.** Si le numéro que lit le logiciel hôte (`install.rdf`,
   `manifest.json`, `description.xml`) n'est pas celui saisi, l'import est refusé : le poste
   annoncerait un autre numéro que la cible.
3. **Signature d'origine**, selon `DM_PACKAGE_SIGNATURE_POLICY`.
4. **Configuration embarquée** (`dm-config.json`) : les valeurs que le DM gouverne (LLM,
   Keycloak) sont remplacées par les siennes, sauf si l'administrateur décoche l'adaptation.
5. **Adaptation du paquet** : configuration retirée, liste de DM de l'instance inscrite, ancre
   de confiance `dm-trust.json` déposée à la racine.
6. **Signature** du paquet tel qu'il sera servi.

## Réglages

| Variable | Où | Rôle |
|---|---|---|
| `DM_PACKAGE_SIGNATURE_POLICY` | tous les composants | `off` (défaut), `warn`, `require` |
| `DM_PACKAGE_PUBLISHER_KEYS` | tous | clés publiques (PEM) des chaînes de build acceptées |
| `DM_PACKAGE_SIGNATURE_OPTIONAL_PLUGINS` | tous | plugins dont la signature d'origine peut être levée à l'envoi (voir plus bas) |
| `DM_PACKAGE_ALLOWED_REFS` | tous | références Git acceptées sans confirmation (`refs/heads/main,refs/tags/*`) |
| `DM_PACKAGE_ALLOWED_REPOSITORIES` | tous | dépôts acceptés (`IA-Generative/*`) |
| `DM_PACKAGE_SIGNING_REQUIRED` | tous | `true` : un composant sans clé refuse l'import |
| `DM_PACKAGE_TRUST_ROOTS` | tous | clés publiques (PEM) de la racine de l'organisation |
| `DM_PACKAGE_SIGNING_DELEGATION` | tous | délégation signée par la racine (JSON). Ce n'est pas un secret |
| `DM_PACKAGE_SIGNING_KEY` | **administration seulement** | clé privée de signature du DM (PEM) |

Chaque variable de clé accepte une variante `_FILE` qui désigne un fichier monté. Dans le
chart : `config.packageTrust.*` pour toutes sauf la clé privée, qui est une clé du Secret de
l'application que seul le déploiement d'administration lit.

Une valeur inconnue de `DM_PACKAGE_SIGNATURE_POLICY` (« required », « on »…) est traitée
comme `require`, avec une erreur au journal : une faute de frappe ne désactive pas le contrôle.

Ces réglages ne se modifient pas depuis l'interface d'administration : une politique de
signature ne doit pas pouvoir être levée par un compte d'administration compromis.

## Mise en service, par paliers

1. **Créer les clés** avec `scripts/package-signing/dm-package-keys.py` (l'en-tête du script
   donne les commandes et dit où va chaque fichier). La clé privée de la racine reste hors
   ligne ; elle ne sert qu'à signer des délégations.
2. **Signer sans exiger.** Poser `DM_PACKAGE_SIGNING_KEY`, la délégation et la racine. Les
   paquets importés ensuite portent l'ancre et sont signés ; la directive de mise à jour
   contient `signature`. Un plugin qui ne sait pas la vérifier l'ignore.
3. **Observer l'origine.** Publier les clés d'éditeur et passer en `warn`. La fiche de chaque
   version dit « Origine vérifiée », « non signée » ou « non vérifiable ».
4. **Exiger.** Quand toutes les chaînes de build publient leur `.sig.json` : `require`, et
   `DM_PACKAGE_SIGNING_REQUIRED=true`. Un envoi automatisé doit alors viser le composant
   d'administration : un pod de service n'a pas la clé et refuse.
5. **Réimporter** les versions en service, pour qu'elles portent l'ancre. Un plugin dont le
   paquet installé contient `dm-trust.json` exige ensuite une signature valide pour toute mise
   à jour ; la confiance s'ancre à l'installation de ce premier paquet.

Renouveler la délégation avant son échéance : un DM dont la délégation a expiré refuse
d'importer (`signing_config`), et les postes refusent les paquets qu'il a signés. Il suffit de
remplacer `DM_PACKAGE_SIGNING_DELEGATION` sur tous les composants : la délégation en vigueur
est jointe à chaque enveloppe au moment où elle est servie, les paquets déjà importés n'ont
pas à l'être de nouveau.

## En attendant qu'une chaîne de build signe

Tant que la CI d'un plugin ne publie pas de `.sig.json`, la politique `require` refuserait
tous ses paquets. `DM_PACKAGE_SIGNATURE_OPTIONAL_PLUGINS` nomme ces plugins (noms au
catalogue, séparés par des virgules ; par exemple `mirai-libreoffice,iassistant-direct`).

Pour eux seulement, l'écran d'envoi affiche une case **« Exiger la signature d'origine »**,
cochée. Un paquet sans signature reste refusé tant qu'elle l'est. L'administrateur la
décoche puis valide : le paquet est importé, la fiche de la version affiche « Origine non
signée (exigence levée) », et le journal d'audit le retient. Par l'API, c'est le champ
`skip_signature=true`.

- Une signature fournie est toujours vérifiée, même pour ces plugins.
- Le DM signe quand même le paquet qu'il sert : seule la preuve d'origine manque.
- La liste se vide plugin par plugin, dès que sa chaîne de build signe.

## Refus à l'import

| Code | HTTP | Sens | Que faire |
|---|---|---|---|
| `signature_required` | 422 | pas de signature d'origine valide, politique `require` | joindre le `.sig.json` publié avec le paquet |
| `signature_invalid` | 422 | la signature fournie ne correspond pas : fichier modifié, mauvais plugin, mauvaise version | reprendre le paquet et sa signature à la source |
| `version_mismatch` | 422 | le paquet se déclare sous un autre numéro | enregistrer sous le numéro du paquet |
| `signature_waivable` | 409 | pas de signature, mais ce plugin est de ceux dont l'exigence peut être levée | décocher « Exiger la signature d'origine » et valider, ou `skip_signature=true` par l'API |
| `ref_not_allowed` | 409 | construit depuis une référence hors liste | confirmer ; la confirmation est tracée |
| `prerelease_ordinary` | 409 | pré-version en campagne ordinaire sur une production | préférer un essai sur une cohorte, ou confirmer |
| `bootstrap_signed` | 409 | archive à signature embarquée dont la liste de DM diffère | confirmer qu'elle est servie telle quelle |
| `signing_config` | 422 | ce composant ne peut pas signer, ou sa configuration est incohérente | importer par l'administration ; vérifier clés et délégation |
| `plugin_required` | 422 | politique `require`, et le chemin d'envoi ne nomme pas le plugin (`/api/artifacts`, ancien formulaire de déploiement, envoi d'artefact nu) | passer par « Nouvelle version » de la fiche du plugin, ou `POST /api/plugins/{slug}/deploy` |
| `unreadable_archive` | 422 | archive endommagée, entrée illisible, ou deux entrées de même nom | reprendre le paquet à la source |
| `foreign_trust_anchor` | 422 | archive à signature embarquée qui apporte son propre `dm-trust.json` | retirer ce fichier du build |

Un refus n'écrit rien : ni fichier, ni version, ni campagne.

## Où lire le résultat

- **Fiche du plugin, onglet Versions** : nature (générale, candidate, essai), origine (branche
  et commit, « sur confirmation » le cas échéant), signature du DM, et qui reçoit la version.
- **Journal d'audit**, action `version.upload` (administration) ou `package.import` (API) :
  contrôle d'origine, référence confirmée hors liste, empreinte du paquet reçu, paquet signé
  ou non. L'enveloppe elle-même n'y est pas recopiée.
- **Catalogue** : `…/download/<fichier>.sig.json` sert l'enveloppe du paquet, pour qui installe
  à la main.
- **Vérifier à la main** : `dm-package-keys.py verify <paquet> <enveloppe> --public <clés>`.

## Ce que cela ne couvre pas

La première installation d'un plugin, la configuration servie par `/config`, les canaux de
mise à jour natifs (flux LibreOffice, manifestes de navigateur), et Thunderbird 31. Voir
l'ADR.
