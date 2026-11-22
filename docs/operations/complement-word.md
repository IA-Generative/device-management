# Complément Word servi par le DM

Le complément Word Éloignement (dépôt IAssistant-Word, dossier `addin/`) est une page web
qu'Office charge depuis l'adresse écrite dans son manifeste. Le DM la sert sous `/addins/word/`,
sur sa propre origine : les appels du complément à `/config`, `/enroll`, `/auth/token` et
`/tools/v1` restent ainsi de même origine, sans CORS.

## Publier une version

| Variable | Rôle |
|---|---|
| `DM_ADDIN_WORD_DIR` | Répertoire qui contient le dossier `addin/` du complément. Vide ou absent : `/addins/word/*` répond 404. |
| `DM_ADDIN_WORD_VERSION` | Version annoncée dans le manifeste, sur quatre nombres (défaut `0.1.0.0`). |

Le complément pèse environ 160 Ko pour 25 fichiers. Le plus simple est donc une ConfigMap
(`binaryData` pour les icônes), montée en lecture seule à l'emplacement de `DM_ADDIN_WORD_DIR`.
Publier une version revient à remplacer son contenu et à monter `DM_ADDIN_WORD_VERSION`. Aucun
artefact signé n'est en jeu : Word recharge les fichiers depuis le DM.

Seuls les types attendus sont servis (`.html`, `.css`, `.js` en `text/javascript`, `.png`,
`.svg`, `.xml`), avec `Cache-Control: no-cache` et une CSP en `Report-Only` :
`default-src 'self'; script-src 'self' https://appsforoffice.microsoft.com; style-src 'self'; img-src 'self' data:; connect-src 'self'`.
`X-Frame-Options: DENY` n'est pas posé sous `/addins/word/`, contrairement au reste du DM, car
Word sur le web charge le volet dans une iframe.

## Le manifeste

`GET /addins/word/manifest.xml?profil=<profil>` rend le manifeste après avoir remplacé :

- `{{BASE}}` par `PUBLIC_BASE_URL` suivi de `/addins/word` ;
- `{{PARAMS}}` par `?profil=<profil>` (vide sans paramètre, c'est-à-dire en production) ;
- `{{KEYCLOAK}}` par l'origine du SSO (`DM_AUTH_ISSUER_URL`, `AUTH_ISSUER_URL` ou
  `KEYCLOAK_ISSUER_URL`) ;
- `{{VERSION}}` par `DM_ADDIN_WORD_VERSION`.

Ce manifeste est ensuite installé sur les postes : chargement latéral pour l'essai, stratégie de
groupe ou MDM ensuite.

## Le client `mirai-word`

C'est un plugin du catalogue comme les autres (slug `mirai-word`, type `word`), sans paquet à
distribuer. Son gabarit de configuration annonce :

- `keycloakIssuerUrl`, `keycloakRealm` et `keycloakClientId`, celui du client public déjà utilisé
  par les autres plugins ;
- l'interrupteur `featureToggles.eloignement`, à `true` sur le profil où la fonction est ouverte.

`/enroll` accepte le `device_name` `mirai-word` sans réglage particulier. Après l'enrôlement,
`/config/mirai-word/config.json` sert `toolsEndpoint` dès que le relais d'outils a un service actif.

## Keycloak

Le client public des plugins, en code d'autorisation avec PKCE S256, reçoit une adresse de
retour de plus : `https://<dm>/addins/word/connexion.html`, exactement, sans paramètre. L'échange
du code passe par le proxy `/auth/token` du DM : aucune origine web n'est à ajouter.

## Ingress

Un DOCX d'acte peut peser 9 Mo, polices embarquées comprises. Le corps accepté par l'ingress doit
couvrir 25 Mo sur `/tools/v1`.
