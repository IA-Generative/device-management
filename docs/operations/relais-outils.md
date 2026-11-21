# Relais d'outils `/tools/v1` — exploitation et configuration

> **Public : opérateur / administrateur DM.** Le besoin et les garde-fous sont dans l'issue #116 ;
> le code fait référence dans [`app/tools/`](../../app/tools/). Même modèle que le
> [proxy LLM](llm-proxy.md) : registre par configuration, identité du poste par les en-têtes relay,
> bornes, journal sans contenu.

Le chat des plugins (Thunderbird, LibreOffice…) appelle des **outils** : chercher dans Mes réunions,
chercher dans Mes collections, lister ou lancer un agent de Mes agents. Plutôt qu'un appel direct du
plugin vers chaque service (une audience SSO par service, joignabilité incertaine depuis le RIE, deux
protocoles à coder dans chaque plugin, aucune gouvernance), le DM **relaie** ces appels : une seule
adresse, un seul format, et l'appel reste **celui de la personne**.

---

## Les routes

| Route | Rôle |
|---|---|
| `GET /tools/v1` | **Découverte.** Les outils que ce poste peut appeler, au format outils OpenAI : `{"tools": [{"type": "function", "function": {"name", "description", "parameters"}, "service": "<id>", "acts": false}], "services": ["<id>", …]}`. Nom public `service__outil` (double souligné : jeu `[a-zA-Z0-9_-]` des noms de fonction OpenAI). `acts: true` = l'outil agit (dépôt, lancement) : le plugin demande confirmation à l'utilisateur, le DM ne confirme rien à sa place. Pour un service MCP, le DM traduit `tools/list` (cache 60 s) et ne garde que les outils de sa liste blanche ; pour un service REST, il sert le schéma du registre. **Un service coupé ou en panne n'est pas listé, les autres restent.** |
| `POST /tools/v1/{service}/{outil}` | **Appel.** Corps `{"arguments": {…}}`. Le DM valide les arguments contre le schéma (jsonschema, Draft 2020-12), appelle l'amont (`tools/call` en MCP, la route déclarée en REST), borne la réponse et rend `200 {"result": <JSON de l'amont>, "truncated": false}`. Pour un amont MCP, `result` est le JSON de `content[0].text` (ou `structuredContent`) ; une erreur d'outil MCP (`isError`) devient une erreur du relais, jamais un 200. |

| `GET` `POST` `PUT` `PATCH` `DELETE` `/tools/v1/{service}/{chemin…}` | **Passthrough.** Pour un service déclaré `protocol: "passthrough"` : la requête est propagée telle quelle vers l'amont, et la réponse amont rendue telle quelle. Voir [Le protocole passthrough](#le-protocole-passthrough). |

**Erreurs** : statut HTTP + `{"error": {"code", "message"}}`, codes `unauthorized`, `forbidden`,
`not_found`, `invalid_arguments`, `rate_limited` (avec `Retry-After`), `unavailable`, `blocked`.
Un `{error:{message}}` de l'amont est repris tel quel (borné à 300 caractères) ; aucun autre
contenu amont n'est recopié dans une erreur.

## Identité : deux en-têtes, deux rôles

| En-tête | Qui | Vérification |
|---|---|---|
| `X-Relay-Client` / `X-Relay-Key`, **ou** `Authorization: Bearer <llmToken>` | le **poste** (enrôlement) | comme sur `/llm/v1` : paire relay vérifiée en base (cible `tools`, accordée aux enrôlements existants comme `llm`), ou `llmToken` signé puis révocation re-vérifiée |
| `X-User-Authorization: Bearer <jeton Keycloak>` | la **personne** | signature, expiration et émetteur (`_verify_access_token`, les réglages `DM_AUTH_*` du DM), puis **transmis tel quel** à l'amont dans `Authorization` |

Le relais **n'appelle jamais en son nom** : pas de clé de service, les trois services filtrent sur
le `sub` de la personne. Deux personnes n'obtiennent pas les mêmes résultats. Aucun autre en-tête
entrant ne passe à l'amont (ni relay, ni cookie).

Ce que l'amont **accepte** dépend de l'audience et de l'`azp` du jeton du plugin : c'est un réglage
Keycloak (portées optionnelles demandées par le plugin, client inscrit chez le service), pas un
réglage du DM. Le registre porte `identity: "passthrough"` (servi) ou `"exchange"` (échange de jeton
Keycloak, réservé : un service qui le déclare est **écarté** avec un avertissement, pour que la V1 ne
mente jamais sur l'identité).

## Le registre — `DM_TOOLS_REGISTRY`

JSON, relu **à chaque requête** (hot-reload par l'onglet Config de l'admin, comme les clés du proxy
LLM). Les adresses des amonts viennent des valeurs de déploiement, **jamais du code**.

```json
{"services": [
  {"id": "mescollections", "label": "Mes collections", "protocol": "mcp",
   "upstream": "https://<hôte>/mcp", "health_path": "/health",
   "timeout_s": 10, "max_response_kb": 256,
   "tools": {"rechercher": {"acts": false}, "trouver_collections": {"acts": false},
             "lister_sources": {"acts": false}, "proposer_sources": {"acts": false}}},
  {"id": "mesreunions", "label": "Mes réunions", "protocol": "rest", "upstream": "https://<hôte>",
   "timeout_s": 10,
   "tools": {"chercher": {"description": "Recherche dans les réunions de la personne", "acts": false,
                          "method": "GET", "path": "/api/v1/search", "query": ["q", "limit", "from", "to"],
                          "parameters": {"type": "object", "additionalProperties": false, "required": ["q"],
                                         "properties": {"q": {"type": "string", "minLength": 1, "maxLength": 1000},
                                                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                                                        "from": {"type": "string"}, "to": {"type": "string"}}}}}},
  {"id": "mesagents", "label": "Mes agents", "protocol": "rest", "upstream": "https://<hôte>",
   "profiles": ["int"],
   "tools": {"lister": {"acts": false, "method": "GET", "path": "/api/v1/agents", "query": ["input", "limit"],
                        "parameters": {"type": "object", "properties": {"input": {"type": "string"},
                                                                        "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
             "lancer": {"acts": true, "method": "POST", "path": "/v1/chat/completions", "body": "json", "timeout_s": 130,
                        "parameters": {"type": "object", "required": ["model", "messages"],
                                       "properties": {"model": {"type": "string"}, "messages": {"type": "array"}}}}}}
]}
```

| Champ | Sens |
|---|---|
| `id` | identifiant du service, `[a-z0-9-]`, 31 caractères au plus ; première moitié du nom public |
| `protocol` | `rest`, `mcp` ou `passthrough` (ce dernier a ses propres champs, [plus bas](#le-protocole-passthrough)) |
| `upstream` | base REST, ou point d'entrée MCP (Streamable HTTP sans état, réponse JSON ou SSE) |
| `tools` | **liste blanche** : seuls ces outils passent. En REST chaque outil porte `method` (`GET`/`POST`), `path` (peut porter des `{param}`), `query` (arguments envoyés en query string), `body: "json"` (les autres arguments en corps JSON), `parameters` (schéma JSON, **requis**). En MCP, seul `acts` compte : le schéma vient de `tools/list`. |
| `acts` | `true` si l'outil agit (dépôt, lancement) : signalé au plugin, qui demande confirmation |
| `identity` | `passthrough` (défaut) ; `exchange` écarte le service (réservé) |
| `auth` | `bearer` (défaut, seul admis) : **un service MCP sans authentification ne peut pas être inscrit** |
| `timeout_s` | délai amont du service (défaut `DM_TOOLS_DEFAULT_TIMEOUT_SECONDS`, plafond 180) ; surchargeable par outil |
| `max_response_kb` | borne de la réponse amont (défaut `DM_TOOLS_MAX_RESPONSE_KB`, plafond 4 Mo) : au-delà, réponse coupée et `truncated: true` |
| `profiles` | si présent, le service n'est servi que si `DM_CONFIG_PROFILE` en fait partie : **un service coupé pour un profil n'est ni listé ni appelable** |
| `health_path` | sonde sans jeton pour le voyant « Relais d'outils » de `/admin/debug` (facultatif) |

## Le protocole passthrough

Les protocoles `rest` et `mcp` exposent des **outils** : une liste blanche de fonctions, des
arguments JSON validés, une réponse JSON bornée. Une application métier complète (ses ressources,
tous les verbes, des dépôts de fichiers) n'entre pas dans ce moule. Le protocole `passthrough`
associe un **nom** de service à l'**adresse** d'une application, et propage tout ce qui suit le nom :

```
PATCH {dm}/tools/v1/eloignement/v1/dossiers/12?x=1
   →  PATCH {base_url}/v1/dossiers/12?x=1        même corps, même Content-Type
```

Le DM ne connaît pas les objets métier. Le corps n'est **jamais analysé** : JSON, `merge-patch`
ou `multipart/form-data` passent octet pour octet. Le plugin ne connaît ni l'adresse ni les
secrets de l'amont.

### Ce que le DM fait au passage

1. **Il vérifie le poste et la personne**, comme pour les outils : `X-Relay-Client` /
   `X-Relay-Key` (ou `llmToken`), et `X-User-Authorization: Bearer <jeton Keycloak>` vérifié par
   `_verify_access_token` (signature par les clés du royaume, expiration, émetteur, audience si
   `DM_AUTH_AUDIENCE` ou `KEYCLOAK_CLIENT_ID` est posée). Sans poste ou sans jeton de personne
   valide : `401`, **rien ne part vers l'amont**.

   > **`DM_AUTH_VERIFY_ACCESS_TOKEN` doit rester à `true`** (son défaut) sur tout DM qui sert un
   > passthrough. À `false`, réglage de développement, le jeton est seulement décodé : un poste
   > enrôlé pourrait alors fabriquer un jeton et se présenter à l'amont sous l'adresse de son choix.
2. **Il applique les bornes du service** : chemin hors des préfixes de `paths` → `404` ; verbe hors
   de `methods` → `405` (avec `Allow`) ; corps au-delà de `max_body_mb` → `413` ; débit par
   personne et par service (`DM_TOOLS_RATE_PER_MINUTE`) → `429`.
3. **Il retire les en-têtes entrants** qui identifient le poste ou la personne : `authorization`,
   `cookie`, `x-relay-*`, `x-user-authorization`, `x-agent-email`, `host`, ainsi que
   `x-client-id` / `x-client-key`, `forwarded`, `x-forwarded-*`, `x-real-ip`, `proxy-*`, les
   en-têtes de transport, et tout en-tête dont le nom porte un tiret bas. Les autres traversent
   (`Content-Type`, `Accept`, `Idempotency-Key`, `If-Match`…).
4. **Il pose l'authentification de l'amont**, selon `auth.kind` (tableau ci-dessous).
5. **Il pose l'identité de l'agent** si le service déclare `agent_header` : la revendication
   `email` du jeton de la personne, dans l'en-tête nommé. Un en-tête de ce nom envoyé par le poste
   est écrasé. Jeton sans adresse exploitable : `403`, aucun appel amont. À la différence des
   services `rest` et `mcp`, le jeton de la personne **n'est pas transmis** à l'amont : l'appel est
   fait au nom du DM, qui atteste l'adresse.
6. **Il rend la réponse amont telle quelle** : code, corps, et les seuls en-têtes `Content-Type`,
   `Location`, `ETag`, `Retry-After`. Une redirection n'est pas suivie, elle est rendue. Une
   réponse au-delà de `max_body_mb` n'est pas coupée mais refusée (`502`).

Une panne réseau de l'amont donne `502` (injoignable) ou `504` (délai), au format d'erreur du
relais, sans adresse ni détail interne. Les refus du DM gardent la forme
`{"error": {"code", "message"}}` : c'est ce qui les distingue d'une erreur métier de l'amont, qui
arrive dans son propre format (`application/problem+json` pour Éloignement).

### Entrée de registre

Dans la liste `services` (avec `id`), ou directement sous le nom du service à la racine du
registre. Les deux formes peuvent cohabiter.

```json
{"services": [
  {"id": "eloignement", "label": "Éloignement", "protocol": "passthrough",
   "base_url": "http://localhost:8000",
   "paths": ["/v1/"], "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
   "max_body_mb": 60, "timeout_s": 120,
   "auth": {"kind": "client_credentials", "token_url": "https://<sso>/realms/<royaume>/protocol/openid-connect/token",
            "client_id": "<client de service>", "secret_env": "ELOIGNEMENT_CLIENT_SECRET"},
   "agent_header": "X-Agent-Email"},
  {"id": "ocr", "label": "Lecture de documents", "protocol": "passthrough",
   "base_url": "https://<hôte async-api>",
   "paths": ["/"], "methods": ["GET", "POST"],
   "max_body_mb": 60, "timeout_s": 120,
   "auth": {"kind": "header", "header_name": "apikey", "secret_env": "ASYNC_API_KEY_ELOIGNEMENT"}}
]}
```

Pour un essai local sans serveur de jetons, `eloignement` peut porter
`"auth": {"kind": "static", "secret_env": "ELOIGNEMENT_JETON_LOCAL"}`. Pour `ocr`, remplacer
`"paths": ["/"]` par les préfixes réellement utilisés dès qu'ils sont connus.

| Champ | Sens |
|---|---|
| `base_url` | adresse de l'application, sans barre finale ; le chemin reçu y est ajouté (`upstream` est accepté comme synonyme) |
| `paths` | **requis.** Préfixes de chemin autorisés. Un préfixe s'arrête à une frontière de segment : `/v1/` et `/v1` ouvrent `/v1/dossiers`, pas `/v1beta`. Sont toujours refusés : les segments `.` et `..` (même encodés), la barre oblique inversée, un `%` restant après décodage (double encodage). `["/"]` ouvre toute l'application : à éviter. |
| `methods` | **requis.** Verbes autorisés parmi `GET`, `POST`, `PUT`, `PATCH`, `DELETE` |
| `max_body_mb` | taille maximale du corps de la requête **et** de la réponse (défaut 10, plafond 100) |
| `timeout_s` | délai de réponse de l'amont (défaut `DM_TOOLS_DEFAULT_TIMEOUT_SECONDS`, plafond 180) |
| `auth` | **requis** : un service sans authentification amont ne s'inscrit pas. Voir ci-dessous. |
| `agent_header` | nom de l'en-tête qui reçoit l'adresse de la personne (facultatif) |
| `label`, `profiles`, `health_path` | comme pour les autres protocoles |

Une entrée mal formée est **écartée entièrement** avec un avertissement dans le journal du DM
(`tools registry: service … refusé`) : elle n'est alors ni annoncée ni appelable.

| `auth.kind` | Ce que le DM pose | Champs |
|---|---|---|
| `client_credentials` | `Authorization: Bearer <jeton de service>`. Le jeton est demandé à `token_url` (`grant_type=client_credentials`, `client_id`, `client_secret`), gardé en mémoire par pod et redemandé 30 s avant l'expiration annoncée ; un `401` de l'amont le fait oublier. | `token_url`, `client_id`, `secret_env` |
| `static` | `Authorization: Bearer <valeur de la variable>` | `secret_env` |
| `header` | `<header_name>: <valeur de la variable>` (par exemple `apikey`) | `header_name`, `secret_env` |

**Le secret n'est jamais dans le registre** : `secret_env` est le *nom* d'une variable
d'environnement du pod (secret Kubernetes, `api.extraEnv` du chart). Variable absente ou vide :
`503`, aucun appel amont, et une ligne d'erreur qui nomme la variable dans le journal du DM.

### Découverte

`GET /tools/v1` annonce les services passthrough servis à ce profil dans une clé à part, sans rien
de leur amont :

```json
{"tools": [], "services": ["eloignement"],
 "passthrough": [{"service": "eloignement", "protocol": "passthrough", "label": "Éloignement",
                  "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"]}]}
```

`toolsEndpoint` est annoncé dans `/config` dès qu'un service, passthrough compris, est servi.

### Limites connues

- Le corps de la requête et celui de la réponse sont **gardés en mémoire** le temps de l'appel
  (pas de flux) : `max_body_mb` borne donc aussi la mémoire prise par appel. À dimensionner avec le
  nombre d'appels simultanés ; l'ingress du DM doit par ailleurs accepter cette taille.
- Pas de réponse en flux (SSE) ni de WebSocket.
- Le cache des jetons de service est local au pod.
- `/admin/debug` affiche un passthrough avec « 0 outil(s) » : il n'en déclare pas.

## Les autres clés

| Clé | Rôle |
|---|---|
| `DM_TOOLS_ENABLED` | **Interrupteur maître** (défaut `false`). À `false`, `/tools/v1` rend une liste vide, aucun appel ne passe, et `/config` annonce `toolsEndpoint: ""`. |
| `PUBLIC_TOOLS_URL` | URL écrite dans `toolsEndpoint`. Défaut : `PUBLIC_BASE_URL` + `/tools/v1`. |
| `DM_TOOLS_RATE_PER_MINUTE` | Appels **par personne et par service** par minute (défaut 60, `0` = sans limite). Compteur mémoire par pod : approximatif en multi-réplicas, suffisant pour une protection ; le `Retry-After` de l'amont est respecté en plus. |
| `DM_TOOLS_DEFAULT_TIMEOUT_SECONDS` | Délai amont par défaut (15 s). Un amont lent rend `unavailable` dans ce délai, sans bloquer les autres services. |
| `DM_TOOLS_MAX_RESPONSE_KB` | Borne de réponse par défaut (256 Ko). |

Dans le chart Helm, ces variables se posent par `api.extraEnv` et `llmProxy.extraEnv` (le relais vit
dans les pods API, et dans les pods `llm` s'ils existent, comme le proxy LLM), ou à chaud depuis
l'admin. Le registre est une chaîne JSON : dans `values.yaml`, un bloc `|-`.

## Ce que voit le plugin — `GET /config/{device}/config.json`

`toolsEndpoint` = l'URL publique du relais quand le relais est actif **et** qu'au moins un service
est servi à ce profil, sinon `""`. Le plugin lit cette seule clé, appelle `GET <toolsEndpoint>` pour
découvrir, puis `POST <toolsEndpoint>/{service}/{outil}`.

### Clés du plugin Éloignement

Le plugin lit aussi, toutes facultatives : `eloignementRecherches` (liste `{id, libelle, url}`,
l'`url` portant `{q}`, `{nom}`, `{prenom}`, `{naissance}` que le plugin remplace),
`eloignementOcrModele`, `eloignementOcrConsigne`, `eloignementIdentiteConsigne`, `eloignementOcrService`,
`eloignementClassementConsigne`, `eloignementDecoupeService`, `eloignementDecoupeSeuilPages`,
`eloignementInterfaceUrl`.

**Le DM n'en porte qu'une, `eloignementOcrModele`** (voir « Modèle de vision » ci-dessous). Pour
les autres, il sert ce que le gabarit du plugin déclare, comme toute autre clé. Elles se posent donc dans le gabarit, par profil (`dm-config.json` du paquet,
sections `default` et par profil, ou fichier `config/<plugin>/config.<profil>.json`), ou par
`DM_CONFIG_OVERRIDES` pour tout un déploiement. Absentes du gabarit, elles sont absentes de la
réponse et la fonction reste éteinte côté plugin. Un repère `${{PUBLIC_BASE_URL}}` y est résolu ;
les accolades simples (`{q}`) sont laissées au plugin.

Exemple pour un profil d'essai, où la recherche pointe vers la page de simulation du DM (aucune
valeur réelle : les consignes, le modèle et l'adresse de l'interface sont à fournir par le métier) :

```json
{"configVersion": 1,
 "default": {},
 "int": {
   "eloignementRecherches": [
     {"id": "simulation", "libelle": "Système simulé",
      "url": "${{PUBLIC_BASE_URL}}/simulation/si/simulation?q={q}&nom={nom}&prenom={prenom}&naissance={naissance}"}
   ],
   "eloignementOcrService": "ocr",
   "eloignementDecoupeSeuilPages": 20
 }}
```

### Modèle de vision d'Éloignement

Le visualiseur lit une page scannée, ou une zone tracée (« Lire comme une identité »), par un modèle
de vision appelé à travers `/llm/v1/chat/completions`. Le nom du modèle se règle au DM, pas dans le
plugin :

| Clé | Rôle |
|---|---|
| `ELOIGNEMENT_VISION_MODEL_NAME` | Nom du modèle annoncé au plugin dans `eloignementOcrModele`. Réglable à chaud dans l'onglet Config de l'admin (groupe LLM). Vide : le DM n'annonce rien de lui-même, la valeur du gabarit s'applique si elle existe ; sans l'une ni l'autre, la lecture par modèle et « Lire comme une identité » sont absentes du visualiseur. |
| `ELOIGNEMENT_OCR_SERVICE_NAME` | Nom du moteur de l'async-api qui lit une pièce entière (« Lire tout le document »), annoncé dans `eloignementOcrService`. Le plugin l'appelle par le service `ocr` du relais d'outils, qui doit être déclaré dans `DM_TOOLS_REGISTRY` (chemins `/v1/`, verbes `GET` et `POST`, clé en `secret_env`). Réglable à chaud. Vide : la commande est absente du visualiseur. Mesure du 2026-11-21 sur un scan fictif de 8 pages : `ocr-lighton` est le plus fidèle (45 s), `ocr-classic` le seul qui ne dépende pas de la carte graphique à la demande. |
| `LLM_BACKENDS` | Si le modèle de vision n'est pas servi par l'amont par défaut, c'est ici qu'on déclare son amont et qu'on y aiguille le modèle. Le jeton reste côté serveur, désigné par `token_env`. |

Exemple, pour deux modèles de vision servis par un amont à part (adresse et noms de modèles fictifs) :

```json
{"backends": {"vision": {"base_url": "https://vision.exemple/api/v1", "token_env": "LLM_API_TOKEN_VISION"}},
 "model_map": {"gemma-*": "vision", "qwen-*": "vision"}}
```

La variable `LLM_API_TOKEN_VISION` se pose au déploiement, dans le Secret, jamais dans le registre
ni dans le dépôt. Changer de modèle (comparer deux modèles au banc) = changer
`ELOIGNEMENT_VISION_MODEL_NAME` : les plugins le reçoivent au prochain relevé de configuration.
Les consignes envoyées avec l'image (`eloignementOcrConsigne`, `eloignementIdentiteConsigne`) restent
dans le gabarit : elles dépendent du modèle choisi.

`GET /simulation/si/{systeme}` est une page statique qui affiche la requête reçue et trois faux
résultats marqués « simulation, données fictives ». Elle n'est servie que si
`DM_SIMULATION_SI_ENABLED` est vrai (défaut : faux, la route répond alors 404) ; elle n'a pas sa
place sur un DM de production. La recherche de l'agent voyage dans l'adresse : elle apparaît donc
dans le journal d'accès du DM, comme elle apparaîtra dans celui de tout système réel appelé ainsi.

## Journal et diagnostic

- Un appel passthrough y figure avec `tool` = verbe et chemin (`POST /v1/dossiers`), sans la
  chaîne de requête, et `acts: true` pour tout verbe autre que `GET`.
- Une ligne JSON par appel sur le logger `dm-tools-audit` : `service`, `tool`, `acts`, `status`,
  `latency_ms`, `size`, `client_uuid`, `sub` (empreinte courte, jamais en clair), `error`,
  `truncated`. **Jamais les arguments ni la réponse** : une requête de recherche ou le corps d'un
  mail n'ont rien à faire dans un journal. Vérifié par un test.
- `/admin/debug`, voyant **Relais d'outils** : désactivé / registre vide / un résumé par service
  (protocole, nombre d'outils, santé HTTP si `health_path`), et le nombre de services hors profil.

## Hors de cette version

Le dépôt de fichier (multipart) par un outil `rest` ou `mcp` (le protocole `passthrough` le
transporte), les réponses en flux, l'échange de jeton Keycloak, un compteur de débit partagé entre
réplicas, et l'exposition du relais lui-même en MCP.
