# Clés d'API — appeler l'API du DM au nom de quelqu'un

L'API du DM n'avait qu'un jeton de service, partagé par tous (`DM_QUEUE_ADMIN_TOKEN`). Une
**clé d'API** est créée pour une personne ou un automate. Elle a un nom, un titulaire, une
portée, une échéance. Elle se révoque seule, et chaque appel est attribué à son titulaire dans
le journal d'audit. Le jeton de service continue de fonctionner.

## Créer une clé

Administration, menu **Clés d'API**, « Créer une clé » :

| Champ | Sens |
|---|---|
| Nom | à quoi sert la clé (« CI du plugin Thunderbird ») |
| Titulaire | l'adresse à qui les appels seront attribués |
| Portée | *Lecture* : consulter. *Écriture* : envoyer une version, créer et piloter une campagne |
| Validité | 1 à 366 jours ; pas de clé sans échéance |
| Plugins autorisés | facultatif ; vide = tous |

La clé est affichée **une seule fois**. Seule son empreinte est conservée : perdue, elle se
remplace. Seule une session d'administration crée ou révoque une clé ; une clé d'API ne peut
pas en créer une autre.

## Renouveler une clé, changer son échéance

Bouton **« Renouveler »** de la ligne d'une clé. On choisit la nouvelle validité, en jours
à partir d'aujourd'hui, et l'un des deux gestes :

- **Avec un nouveau secret** (case cochée, le défaut). L'ancienne clé cesse de fonctionner
  aussitôt ; la nouvelle est affichée une seule fois, à mettre à sa place. C'est la rotation.
- **Sans nouveau secret** (case décochée). Seule l'échéance change : la clé en service reste la
  même, rien n'est à redéployer. Pour prolonger, ou pour raccourcir.

La clé garde son identifiant, son nom, son titulaire, sa portée et ses plugins. Une clé
expirée se renouvelle et reprend du service. Une clé révoquée ne se renouvelle pas : la
révocation est définitive. Le renouvellement est tracé au journal d'audit (`api_key.renew`).

Il n'y a pas de période de recouvrement. Pour une rotation sans coupure : créer d'abord une
seconde clé, basculer l'automate, puis révoquer la première.

## L'utiliser

```
curl -H "Authorization: Bearer $DM_API_KEY" https://<dm>/api/campaigns

curl -H "Authorization: Bearer $DM_API_KEY" \
     -F binary=@mirai-matisse-0.25.0.xpi -F signature=@mirai-matisse-0.25.0.xpi.sig.json \
     -F strategy=canary \
     https://<dm>/api/plugins/mirai-matisse/deploy
```

L'en-tête `X-Admin-Token: <clé>` est accepté aussi, pour les scripts qui l'utilisent déjà.

## Ce qu'une clé ouvre

Les mêmes routes que le jeton de service, pas davantage :

| Route | Portée |
|---|---|
| `GET /api/campaigns`, `GET /api/campaigns/{id}/progress` | lecture |
| `POST /api/plugins/{slug}/deploy`, `POST /api/artifacts` | écriture |
| `POST /api/campaigns`, `PATCH /api/campaigns/{id}/start\|pause\|resume\|abort` | écriture |
| `POST /admin/catalog/{id}/versions/upload`, `…/general-version`, `…/general-campaign`, `…/deploy-preview`, `…/candidate-visibility`, `…/versions/{vid}/invite`, `POST /admin/api/catalog/{id}/config-template` | écriture |

Une clé bornée à des plugins ne peut écrire que sur les routes qui nomment l'un d'eux. Elle
est donc refusée sur `/api/artifacts` et sur les routes de campagne, qui n'en nomment aucun.

L'API ne fait pas tout ce que fait l'interface : cohortes, communications, bascules de
fonctions et gestion des clés restent à l'écran.

## Refus

| Code | HTTP | Sens |
|---|---|---|
| `invalid_key` | 401 | clé inconnue ou mal formée |
| `expired_key` | 401 | échéance dépassée : en créer une nouvelle |
| `revoked_key` | 401 | clé révoquée |
| `insufficient_scope` | 403 | clé en lecture sur un appel d'écriture |
| `plugin_not_allowed` | 403 | clé bornée à d'autres plugins |

## Bonnes pratiques

- Une clé par usage, nommée pour ce qu'elle fait : on révoque celle-là, pas les autres.
- Bornée aux plugins qu'elle sert, et en lecture seule quand cela suffit.
- Dans un coffre ou un secret de CI ; jamais dans un dépôt ni un message.
- La colonne « Dernier usage » dit quelles clés ne servent plus.
