# ADR-0006 : Chaîne de confiance des paquets — build neutre, signature d'origine, signature du DM

**Date** : 2026-11-15
**Statut** : Proposé — trois points restent à trancher (cf. « Questions ouvertes »)
**Auteurs** : eric.tiquet + Claude
**Portée** : import d'un paquet (`app/services/package_import.py`, `package_signing.py`,
`package_bootstrap.py`), directive de mise à jour (`app/main.py`), catalogue
(`app/catalog/`), dépôts des plugins (workflow de release, vérification au téléchargement).

---

## Contexte

Un DM réécrit les paquets qu'il sert : il en retire `dm-config.json`, et depuis l'issue
#133 il y inscrit sa propre liste de DM. La cible (issues #106, #107, #108) va plus loin :
la CI publie **un seul paquet neutre par version**, sans valeur d'organisation, et chaque
DM l'adapte à l'import.

Un composant qui réécrit du code distribué à tout un parc est une cible. Aujourd'hui :

- le paquet publié par la CI n'est pas signé ; rien ne prouve à un DM d'où il vient ;
- l'empreinte que vérifie le plugin vient du même DM que le paquet : elle protège d'une
  corruption, pas d'un stockage ou d'un pod de service compromis ;
- un administrateur peut téléverser n'importe quel fichier.

Contraintes : le DM de production n'a pas d'accès sortant libre (proxy en liste blanche),
donc une preuve vérifiable seulement en ligne ne convient pas ; le plugin Thunderbird
tourne sur Gecko 60, qui offre WebCrypto (ECDSA P-256) mais pas Ed25519.

## Décision

Chaque passage de main est signé et vérifié, avec **un seul format**.

### Format

Algorithme unique : **ES256** (ECDSA P-256, SHA-256), signature brute `r||s` (64 octets),
celle que produit et vérifie WebCrypto. `keyid` = 16 premiers caractères hexadécimaux du
SHA-256 de la clé publique au format SPKI DER.

**Enveloppe** (`dm-package-signature/1`), un fichier JSON détaché :

```json
{
  "schema": "dm-package-signature/1",
  "payload": "<base64 de la déclaration, octets exacts signés>",
  "signatures": [{"keyid": "…", "alg": "ES256", "sig": "<base64 r||s>"}],
  "delegation": { "payload": "<base64>", "signatures": [ … ] }
}
```

La signature porte sur les **octets du payload décodé** : aucune canonisation JSON.
`delegation` est facultatif (cf. « Clés »).

**Déclaration** (`dm-package-statement/1`), contenu du payload :

| Champ | `kind: "build"` (CI) | `kind: "served"` (DM) |
|---|---|---|
| `slug`, `version`, `file` | nom du plugin au catalogue, version, nom du fichier | idem |
| `sha256`, `size` | du paquet publié | du paquet **servi** (après adaptation) |
| `source` | `repository`, `ref`, `commit`, `workflow`, `run_id` | — |
| `built_at` / `signed_at` | date ISO | date ISO |
| `dm` | — | adresse publique du DM signataire |
| `upstream` | — | `sha256` du paquet reçu, `verified` (bool), `ref`, `keyid` |
| `changed_files` | — | fichiers de l'archive modifiés ou ajoutés par le DM |
| `extension_id` | — | identifiant de l'extension dans le logiciel hôte, lu dans le paquet (`em:id`, `gecko.id`, `identifier`) ; absent s'il est illisible |

Le `slug` est le nom du plugin au catalogue du DM. Le poste, lui, peut désigner le
plugin par un alias (`matisse` pour `mirai-matisse`) : il ne peut donc pas comparer ce
champ de façon fiable. C'est `extension_id` qui lie la signature à « une mise à jour de
moi ». À l'import, le nom signé par la CI est accepté s'il est le nom au catalogue ou
l'un de ses alias.

### Les quatre maillons

1. **Build.** Le workflow de release du plugin produit le paquet neutre
   `<slug>-X.Y.Z[-rc.N|-dev.N].<ext>` et l'enveloppe `<fichier>.sig.json` (`kind: build`),
   attachés à la release GitHub. La clé privée est un secret d'**environnement** GitHub,
   réservé aux références protégées.
2. **Import.** Le DM vérifie l'enveloppe contre ses clés d'éditeur
   (`DM_PACKAGE_PUBLISHER_KEYS`) : signature, `sha256` du fichier reçu, `slug`, `version`.
   Politique `DM_PACKAGE_SIGNATURE_POLICY` :
   - `off` (défaut) : aucun contrôle, comportement antérieur ;
   - `warn` : contrôle si une enveloppe est fournie, résultat affiché et tracé ;
   - `require` : refus sans enveloppe valide, y compris par téléversement manuel.

   `DM_PACKAGE_ALLOWED_REFS` (motifs, ex. `refs/heads/main,refs/tags/*`) borne les
   références acceptées sans confirmation. Hors liste : confirmation explicite, tracée
   dans le journal d'audit.
3. **Adaptation.** Le DM retire la configuration embarquée, inscrit sa liste de DM
   (#133), dépose `dm-trust.json` à la racine de l'archive, puis signe le résultat
   (`kind: served`) avec `DM_PACKAGE_SIGNING_KEY`. L'enveloppe et la provenance sont
   conservées avec l'artefact (`artifacts.signature`, `artifacts.provenance`).
4. **Poste.** La directive de mise à jour porte l'enveloppe (`update.signature`) ; le
   catalogue la sert à côté du paquet (`…/download/<fichier>.sig.json`). Le plugin
   vérifie avant d'installer : signature, `sha256` des octets téléchargés, `version` égale
   à la cible annoncée, et `extension_id` égal à son propre identifiant. Si la déclaration
   n'a pas d'`extension_id`, il compare le `slug` au nom sous lequel il interroge le DM.
   Sans signature valide, il n'installe pas et le signale (`POST /update/status`).

### Clés

- **Racine de l'organisation** : clé hors ligne. Elle signe une **délégation**
  (`dm-signing-delegation/1` : `dm`, `keyid`, `public_key` SPKI en base64, `not_before`,
  `not_after`) pour la clé de chaque DM. Le DM joint sa délégation à chaque enveloppe.
- **Ancre du plugin** : `dm-trust.json` (`dm-trust/1`), déposé par le DM dans le paquet :
  `{"roots": [{"keyid", "alg", "public_key"}]}`. Le plugin accepte une enveloppe signée
  par une racine, ou par une clé couverte par une délégation valide d'une racine.
- **Mode simple** (intégration, banc) : sans racine ni délégation configurées, l'ancre
  déposée est la clé publique du DM lui-même.
- **La clé du DM n'est montée que sur le composant d'administration**, qui est le seul à
  importer. Les pods de service ne peuvent que relayer une enveloppe déjà produite.
- **La délégation en vigueur est jointe au moment de servir** (directive et `.sig.json`),
  pas figée à l'import : la renouveler ne demande pas de réimporter les paquets. Elle n'est
  pas secrète, tous les composants la lisent.
- **Une signature de build n'est jamais acceptée par délégation.** La délégation ne dit pas
  pour quel usage elle vaut ; elle ne sert qu'à la signature d'un paquet servi. Une
  déclaration de build doit nommer le plugin et la version.
- **Règle de transition** : un plugin dont le paquet installé contient un `dm-trust.json`
  exige une signature valide pour toute mise à jour. Sans ce fichier (paquet antérieur),
  il garde le contrôle par empreinte. La confiance s'ancre donc à l'installation du
  premier paquet signé, et un DM ne peut pas la désactiver à distance.

### Ce que cela ne couvre pas

- **La première installation** : aucun plugin n'est là pour vérifier. Elle passe par le
  catalogue du DM ou la télédistribution.
- **La configuration** servie par `/config`, dont la liste de DM diffusée : elle ne repose
  que sur TLS.
- **Les canaux natifs** (flux LibreOffice, manifestes de navigateur) : le logiciel hôte
  installe sans passer par le plugin. Les extensions de navigateur signées par l'éditeur
  du navigateur ne peuvent pas être réécrites.
- **Thunderbird 31** : pas de WebCrypto ECDSA ; la vérification n'y est pas disponible.

## Alternatives écartées

- **Attestation de provenance GitHub (Sigstore) seule.** Sans clé à garder, liée à
  l'identité du workflow : c'est la meilleure preuve d'origine. Mais sa vérification
  demande l'accès à la racine de confiance Sigstore, que le DM de production n'a pas, et
  le plugin ne sait pas la vérifier. Elle peut s'ajouter plus tard **en plus** de
  l'enveloppe, pour les DM qui ont un accès sortant.
- **Signature embarquée au format Mozilla (`META-INF`).** Exige une autorité que nous ne
  maîtrisons pas, et n'existe ni pour les OXT ni pour les archives génériques.
- **Ed25519.** Absent de WebCrypto sur Gecko 60.
- **Faire signer le paquet adapté par la CI.** Supposerait un build par DM, ce que le
  paquet neutre supprime.

## Conséquences

- Le cas « paquet signé » de #133 change de sens : la signature d'origine est détachée,
  donc la réécriture ne la casse pas ; le DM la vérifie avant, puis signe après. Seules
  les archives à signature **embarquée** restent non réécrivables.
- Un DM compromis reste capable de signer : la délégation bornée dans le temps et la
  séparation du composant d'administration limitent la fenêtre et la surface.
- Le rejeu d'une ancienne version signée est contenu par le refus du plugin de reculer.

## Questions ouvertes

1. Qui détient la racine de l'organisation, et où est-elle conservée ?
2. Durée de validité d'une délégation (proposé : 12 mois, renouvellement à 9).
3. Garde-t-on la vérification par empreinte seule pour Thunderbird 31, ou retire-t-on
   cette cible des mises à jour automatiques ?
