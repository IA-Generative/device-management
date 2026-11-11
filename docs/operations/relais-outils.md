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

## Les deux routes

| Route | Rôle |
|---|---|
| `GET /tools/v1` | **Découverte.** Les outils que ce poste peut appeler, au format outils OpenAI : `{"tools": [{"type": "function", "function": {"name", "description", "parameters"}, "service": "<id>", "acts": false}], "services": ["<id>", …]}`. Nom public `service__outil` (double souligné : jeu `[a-zA-Z0-9_-]` des noms de fonction OpenAI). `acts: true` = l'outil agit (dépôt, lancement) : le plugin demande confirmation à l'utilisateur, le DM ne confirme rien à sa place. Pour un service MCP, le DM traduit `tools/list` (cache 60 s) et ne garde que les outils de sa liste blanche ; pour un service REST, il sert le schéma du registre. **Un service coupé ou en panne n'est pas listé, les autres restent.** |
| `POST /tools/v1/{service}/{outil}` | **Appel.** Corps `{"arguments": {…}}`. Le DM valide les arguments contre le schéma (jsonschema, Draft 2020-12), appelle l'amont (`tools/call` en MCP, la route déclarée en REST), borne la réponse et rend `200 {"result": <JSON de l'amont>, "truncated": false}`. Pour un amont MCP, `result` est le JSON de `content[0].text` (ou `structuredContent`) ; une erreur d'outil MCP (`isError`) devient une erreur du relais, jamais un 200. |

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
| `protocol` | `rest` ou `mcp` |
| `upstream` | base REST, ou point d'entrée MCP (Streamable HTTP sans état, réponse JSON ou SSE) |
| `tools` | **liste blanche** : seuls ces outils passent. En REST chaque outil porte `method` (`GET`/`POST`), `path` (peut porter des `{param}`), `query` (arguments envoyés en query string), `body: "json"` (les autres arguments en corps JSON), `parameters` (schéma JSON, **requis**). En MCP, seul `acts` compte : le schéma vient de `tools/list`. |
| `acts` | `true` si l'outil agit (dépôt, lancement) : signalé au plugin, qui demande confirmation |
| `identity` | `passthrough` (défaut) ; `exchange` écarte le service (réservé) |
| `auth` | `bearer` (défaut, seul admis) : **un service MCP sans authentification ne peut pas être inscrit** |
| `timeout_s` | délai amont du service (défaut `DM_TOOLS_DEFAULT_TIMEOUT_SECONDS`, plafond 180) ; surchargeable par outil |
| `max_response_kb` | borne de la réponse amont (défaut `DM_TOOLS_MAX_RESPONSE_KB`, plafond 4 Mo) : au-delà, réponse coupée et `truncated: true` |
| `profiles` | si présent, le service n'est servi que si `DM_CONFIG_PROFILE` en fait partie : **un service coupé pour un profil n'est ni listé ni appelable** |
| `health_path` | sonde sans jeton pour le voyant « Relais d'outils » de `/admin/debug` (facultatif) |

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

## Journal et diagnostic

- Une ligne JSON par appel sur le logger `dm-tools-audit` : `service`, `tool`, `acts`, `status`,
  `latency_ms`, `size`, `client_uuid`, `sub` (empreinte courte, jamais en clair), `error`,
  `truncated`. **Jamais les arguments ni la réponse** : une requête de recherche ou le corps d'un
  mail n'ont rien à faire dans un journal. Vérifié par un test.
- `/admin/debug`, voyant **Relais d'outils** : désactivé / registre vide / un résumé par service
  (protocole, nombre d'outils, santé HTTP si `health_path`), et le nombre de services hors profil.

## Hors de cette version

Le dépôt de fichier (multipart), les réponses en flux, l'échange de jeton Keycloak, un compteur de
débit partagé entre réplicas, et l'exposition du relais lui-même en MCP.
