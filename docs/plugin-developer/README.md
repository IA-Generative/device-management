# Développer un plugin pour Device Management

> **Pour vous** si vous écrivez une extension bureautique (LibreOffice, Thunderbird, Firefox,
> Chrome/Edge) qui doit s'enrôler, se configurer, se mettre à jour, appeler un modèle de langage
> et remonter de la télémétrie via Device Management (« DM »).
>
> **Trois documents, dans cet ordre.** Ne les lisez pas tous : suivez le parcours.

---

## En trente secondes

Votre plugin a **quatre** interactions avec DM, et une seule est permanente : la boucle de
configuration. Elle distribue tout le reste — y compris les clés d'accès au modèle.

```
  ① S'ENRÔLER — une fois, puis à renouveler
     login SSO Keycloak (OIDC + PKCE)
                 └─ scope=openid offline_access  ← jeton HORS-LIGNE, sinon
                    la session SSO expire et l'utilisateur doit se reconnecter
     plugin ──── POST /enroll  (Bearer <jeton Keycloak>) ────► DM
     plugin ◄─── credentials de relais (30 jours) ───────────  DM
                 └─ à joindre désormais à TOUT appel
                 └─ à expiration : ré-enrôler, donc reprendre en haut
                          │
                          ▼
  ② SE CONFIGURER — en boucle, c'est le cœur
     plugin ──── GET /config/<plugin>/config.json ───────────► DM
                 X-Client-UUID · X-Plugin-Version
                 X-Relay-Client · X-Relay-Key
     plugin ◄─── { config, features, update } ───────────────  DM
                 ├─ config   → dont llmEndpoint + llmToken
                 ├─ features → vos flags, à remplacer EN BLOC
                 └─ update   → une mise à jour, ou null
                               si ≠ null : télécharger, vérifier
                               l'empreinte, installer, puis
                               POST /update/status
                          │
                          ▼
  ③ APPELER LES MODÈLES — avec ce que ② vient de fournir
     génération (dialogue) :
     plugin ──── POST {llmEndpoint}/chat/completions ────────► DM ──► LLM
                 Authorization: Bearer {llmToken}
     embeddings (recherche sémantique) :
     plugin ──── POST {embdUrl}/embeddings ──────────────────► DM ──► LLM
                 Bearer {embdToken} · model: {embdModel}
                 └─ même relais, même jeton, même quota
                 └─ jamais le fournisseur en direct :
                    sa clé ne descend pas sur le poste
                          │
                          ▼
  ④ RENDRE COMPTE — en continu
     plugin ──── POST /telemetry/v1/traces ──────────────────► DM
                 usage · mises à jour · erreurs du relais LLM
                 └─ obligatoire, pas optionnel
```

**Trois idées à retenir dès maintenant :**

- **① conditionne tout, et ① recommence.** Sans credentials de relais, la config arrive amputée de
  ses secrets, `llmToken` est vide et `POST /update/status` répond 401. Ces credentials expirent
  au bout de 30 jours : le ré-enrôlement exige un jeton Keycloak encore valide, ce qui **impose un
  jeton hors-ligne** (`scope=openid offline_access`) — un refresh token ordinaire meurt avec la
  session SSO, bien avant.
- **② est la seule source de vérité.** L'adresse du modèle, les flags, la directive de mise à jour
  changent à chaud, côté serveur. Relisez-les à chaque tour — ne figez rien en préférence.
- **④ n'est pas optionnel.** La journalisation fonctionnelle est ce qui permet de savoir ce que
  vivent réellement les agents : combien butent sur un quota, réessaient, abandonnent.

---

## Le parcours

### Étape 1 — Brancher les appels de base

📖 **[consumer-readme.md](consumer-readme.md)** — *démarrez ici*

Les plugins supportés, le flux d'intégration, puis chaque endpoint avec ses en-têtes et un exemple
cURL : configuration, enrôlement OIDC/PKCE, télémétrie, relais, mises à jour, communications.
Vous en sortez avec un plugin qui parle à DM.

### Étape 2 — Implémenter les mises à jour correctement

📖 **[plugin-dm-protocol-update-features.md](plugin-dm-protocol-update-features.md)** — *le contrat*

C'est **le** document de référence : ce que DM s'engage à émettre, ce que votre client s'engage à
faire. Il fait foi des deux côtés. N'en lisez que trois parties pour commencer :

| Lisez | Vous y trouvez |
|---|---|
| **§0 — les 8 obligations** | une page. Si vous ne lisez qu'une seule chose, c'est celle-là |
| **Annexe — pseudo-code** | le squelette d'un client conforme, les pièges déjà rencontrés en vrai, et une checklist de tests |
| **§2 bis — fichiers `dm-*.json`** | ce que vous devez mettre dans votre archive |

Le reste (§1 à §9) justifie et détaille — à consulter quand une question précise se pose.

### Étape 2 bis — Si votre plugin appelle un modèle, ou remonte de la télémétrie

Deux sujets transverses, traités dans le même document que l'étape 2 :

| Sujet | Où | L'essentiel |
|---|---|---|
| **Accès aux modèles** — génération *et* embeddings | [contrat §4 bis](plugin-dm-protocol-update-features.md) | vous n'appelez jamais le fournisseur d'inférence directement : vous appelez le relais du DM. Son adresse (`llmEndpoint`) et son jeton (`llmToken`) arrivent dans la config et **changent sans prévenir** — relisez-les à chaque poll, ne les codez pas en dur. Les embeddings passent par le **même** relais et le **même** jeton ; seul `embdModel` leur est propre, et **s'il change, vos index vectoriels sont à refaire** |
| **Journalisation fonctionnelle** | [contrat §8 et §8 bis](plugin-dm-protocol-update-features.md) | ce que votre plugin **doit** remonter : les événements de mise à jour, et les erreurs du relais LLM (quota, refus, panne). C'est une obligation, pas une bonne pratique — sans elle, personne ne sait ce que vivent les utilisateurs |

La télémétrie elle-même (endpoint, jeton, rotation, ce qui est permis avant login) est décrite à
l'[étape 1](consumer-readme.md) § 4.

### Étape 3 — Empaqueter et publier

📖 **[packaging-guide.md](packaging-guide.md)**

Comment DM détecte automatiquement votre plugin : les deux fichiers de métadonnées
(`dm-manifest.json`, `dm-config.json`), la structure d'archive pour chaque plateforme, la
détection de version et d'icône, et l'upload.
Gabarit à copier : [config.default.example.json](config.default.example.json).

### Et ensuite — faire déployer votre version

📖 **[../operations/mode-operatoire-campagnes.md](../operations/mode-operatoire-campagnes.md)**

Côté administrateur, pas côté vous — mais c'est ce qui déterminera *qui* reçoit votre version et
*quand*. Utile pour dialoguer avec l'exploitant, et indispensable si vous voulez éprouver une
version sur quelques postes avant de généraliser (§3.3 et §9).

---

## Je cherche une réponse précise

| Question | Où |
|---|---|
| Quels en-têtes envoyer, exactement ? | [contrat §2](plugin-dm-protocol-update-features.md) |
| Pourquoi mon plugin n'est-il jamais mis à jour ? | [contrat §0](plugin-dm-protocol-update-features.md), obligation 1 |
| Pourquoi boucle-t-il sur la même mise à jour ? | [contrat §0](plugin-dm-protocol-update-features.md), obligations 3 et 6 |
| Mon checksum ne correspond jamais | [contrat §2 bis](plugin-dm-protocol-update-features.md) — les `dm-*.json` sont retirés du binaire avant publication |
| Comment déclarer mes feature flags ? | [packaging-guide](packaging-guide.md) → `featureToggles`, puis [contrat §4.4](plugin-dm-protocol-update-features.md) |
| Comment tester une version sur quelques postes ? | [contrat §6 bis](plugin-dm-protocol-update-features.md) |
| Puis-je ajouter un champ sans casser les anciens clients ? | [contrat §9](plugin-dm-protocol-update-features.md) |
| Comment brancher le SSO Keycloak ? | [consumer-readme](consumer-readme.md) § « Keycloak : Authorization Code + PKCE » |
| Mon utilisateur doit se reconnecter tout le temps | Il vous manque `scope=openid offline_access` — [consumer-readme](consumer-readme.md) § « Renouvellement des jetons » |
| Combien de temps durent mes credentials de relais ? | 30 jours — [consumer-readme](consumer-readme.md) § « Renouvellement des jetons » |
| Que puis-je émettre en télémétrie avant login ? | [consumer-readme](consumer-readme.md) § 4 |
| Comment appeler le modèle depuis mon plugin ? | [contrat §4 bis](plugin-dm-protocol-update-features.md) |
| Comment faire des embeddings / de la recherche sémantique ? | [contrat §4 bis](plugin-dm-protocol-update-features.md) § Embeddings (RAG) |
| `embdModel` est vide, que faire ? | L'embedder est désactivé — [contrat §4 bis](plugin-dm-protocol-update-features.md) |
| Le modèle d'embedding a changé, mes index sont-ils valides ? | Non, ré-indexez — [contrat §4 bis](plugin-dm-protocol-update-features.md) |
| Le LLM me renvoie 401 / 403 / 429, je fais quoi ? | [contrat §4 bis](plugin-dm-protocol-update-features.md), table des erreurs |
| Que dois-je journaliser, et sous quelle forme ? | [contrat §8](plugin-dm-protocol-update-features.md) (mises à jour) et [§8 bis](plugin-dm-protocol-update-features.md) (erreurs LLM) |
| Puis-je garder l'URL du LLM en préférence ? | Non — [contrat §4 bis](plugin-dm-protocol-update-features.md) |

---

## Hors parcours

Ces documents ne s'adressent **pas** au développeur de plugin. Ils sont ici parce qu'ils
concernent la même API publique.

| Document | Public |
|---|---|
| [mirai-integration-README.md](mirai-integration-README.md) | Intégrateur d'un **portail tiers** qui veut afficher le catalogue DM |
| [mirai-catalog-snippet.html](mirai-catalog-snippet.html) | idem — snippet DSFR prêt à coller |

---

## Pour aller plus loin

- **Pourquoi c'est conçu ainsi** : [../architecture/adr-0001-vue-densemble.md](../architecture/adr-0001-vue-densemble.md)
  (vue d'ensemble), puis [adr-0004](../architecture/adr-0004-branches-experimentation.md) pour la
  cohabitation de plusieurs versions.
- **Exploiter le service** : [../operations/](../operations/).
- **Vue produit** : [le README racine](../../README.md).
