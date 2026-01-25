# device-management



device-management

Service préliminaire de device management pour l’écosystème iAssistants.

Objectif :
	
    1.	Enrôler des postes / devices (création d’une identité device, association à un utilisateur/organisation, statut)

	2.	Distribuer et récupérer des préférences de configuration (policies) consommées par les iAssistants (extensions, agents, clients desktop, etc.)

⚠️ Ce dépôt est une base de travail : API, schémas, sécurité et flux d’authentification sont susceptibles d’évoluer.

⸻

Pourquoi ce service ?

Les iAssistants ont besoin d’un point central pour :
	•	connaître quels devices sont autorisés (enrôlement / révocation),
	•	récupérer une configuration cohérente et administrable (endpoints, modèles, features flags, paramètres non sensibles),
	•	appliquer une logique fail-closed côté client quand la configuration est absente/invalide.

⸻

Concepts

Device

Un device représente un poste ou une instance logicielle :
	•	identifiant unique (ex: device_id)
	•	métadonnées (type, OS, version client, empreinte, etc.)
	•	statut : PENDING / ENROLLED / REVOKED / BLOCKED
	•	association (optionnelle) à un utilisateur, un groupe, une organisation

Preferences / Policy

Une policy représente la configuration distribuée :
	•	paramètres généraux (feature flags, UX)
	•	paramètres techniques non sensibles (URLs de service, options de capture, quotas)
	•	paramètres sensibles à exclure (tokens, secrets) ou à gérer via un mécanisme dédié (vault / mémoire côté client)

⸻

Flux fonctionnels (cible)

1) Enrôlement (happy path)
	1.	Le client génère / présente un identifiant device (ou un challenge)
	2.	Le service crée un device en PENDING
	3.	Un administrateur ou un mécanisme d’approbation valide → ENROLLED
	4.	Le client peut appeler la récupération de préférences et démarrer

2) Récupération de configuration
	1.	Le client appelle GET /config (ou endpoint équivalent)
	2.	Le service renvoie la policy applicable (par user/device/groupe)
	3.	Le client applique la config et journalise les changements

3) Révocation
	•	Passage à REVOKED (ou BLOCKED) → le client doit se désactiver / réduire les fonctionnalités

⸻

API (préliminaire)

Les routes exactes peuvent varier : cette section sert de contrat d’intention.

Enrôlement
	•	POST /devices/enroll/init
Crée une demande d’enrôlement (device → PENDING)
	•	POST /devices/enroll/confirm
Valide l’enrôlement (PENDING → ENROLLED)

Gestion device
	•	GET /devices/{device_id}
Détails et statut
	•	POST /devices/{device_id}/revoke
Révocation
	•	GET /devices?search=...
Recherche / listing

Configuration iAssistants
	•	GET /iassistants/config?device_id=...
Retourne la config applicable (résolution par priorité)
	•	GET /iassistants/config/version
Version de policy / ETag pour cache client
	•	POST /iassistants/config/ack
(Optionnel) accusé de réception côté client (audit)

⸻

Modèle de configuration (exemple)

{
  "enabled": true,
  "version": "2026-01-25.1",
  "config": {
    "owuiEndpoint": "https://chat.mirai.interieur.gouv.fr/api/",
    "owuiModel": "self-hosted.llama-3.3-70b",
    "featureFlags": {
      "meetingCapture": true,
      "dailySummary": false
    },
    "limits": {
      "maxAudioMinutes": 120
    }
  }
}

Recommandations :
	•	renvoyer un ETag ou version pour permettre au client d’éviter des fetch inutiles
	•	documenter les champs “contractuels” et ignorer le reste côté client

⸻

Sécurité (principes)
	•	Fail-closed : si pas de config valide → client désactivé (ou mode dégradé très limité)
	•	AuthN/AuthZ : à terme via OIDC/Keycloak (user) + attestation device (mTLS, JWT device, ou challenge signé)
	•	Journaux d’audit : enrôlement, révocation, changements de policy
	•	Pas de secrets dans la policy : tokens et clés doivent être gérés via un circuit séparé (vault, mémoire locale chiffrée, etc.)

⸻

Installation & exécution

À compléter selon la stack retenue (FastAPI, Node, Spring, etc.).

Exemple (placeholder) :

# 1) installer dépendances
# 2) configurer env
# 3) lancer le service

Variables d’environnement typiques (placeholder) :
	•	DATABASE_URL
	•	OIDC_ISSUER_URL
	•	OIDC_AUDIENCE
	•	LOG_LEVEL

⸻

Tests

Objectifs de tests (prioritaires) :
	•	enrôlement : PENDING → ENROLLED, refus, révocation
	•	résolution de policy : par device/user/groupe, règles de priorité
	•	conformité JSON : validation schéma et compat ascendante

⸻

Roadmap

Court terme
	•	Schéma de données minimal (Device, Policy, Assignment)
	•	Endpoints d’enrôlement + statut
	•	Endpoint GET config avec versionning/ETag
	•	Audit log minimal

Moyen terme
	•	Intégration OIDC (Keycloak)
	•	Règles de policy (priorités : device > user > group > global)
	•	UI d’administration (listing, approbation, révocation)

Long terme
	•	Attestation forte device (mTLS, cert, TPM, etc.)
	•	Segmentation par tenant/organisation
	•	Distribution différenciée (canary, ring, rollout progressif)

⸻

Statut

Ce dépôt contient une technologie préliminaire : le design est volontairement simple, pour valider les flux “enrôlement + config” et converger vers un socle industrialisable.
