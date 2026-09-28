# Changelog

Ce journal dit ce qui change pour les personnes qui utilisent ou exploitent le service, version par
version, la plus récente en tête. Il est servi tel quel par le service lui-même, sans authentification :
il ne porte ni détail d'infrastructure ni détail technique fin (ceux-là vivent dans les commits et la
documentation). Historique reconstitué le 2026-09-28 depuis les commits, puis reformulé ; les versions
suivantes seront tenues par release-please. Titres au format `## [X.Y.Z] - date`.

## [0.9.20] - 2026-09-26

La 0.9.19 a été publiée depuis une branche de release ; ses changements sont repris ici.

### Nouveautés

* Chaque extension a désormais une **version générale** : celle que reçoivent tous les postes qui ne sont pas dans une expérimentation. L'administrateur la choisit dans la fiche de l'extension.
* LibreOffice peut vérifier lui-même s'il existe une mise à jour d'une extension, par son mécanisme natif ; le catalogue lui répond.
* Les **communications** (annonces, alertes, sondages) parviennent aux extensions installées, qui peuvent en accuser réception. Elles se ciblent par cohorte et par version.
* La fiche d'une extension permet de renseigner son identifiant d'extension LibreOffice.

### Corrections

* Le mécanisme de mise à jour natif de LibreOffice n'annonce qu'une version réellement disponible, et le fait de façon plus sûre.
* Un accusé de réception n'est plus accepté sur une communication encore en brouillon.
* Le suivi d'une campagne compte correctement un poste qui a différé sa mise à jour : il n'est plus compté en échec.
* Diverses corrections issues de la revue de qualité.

### Pour les développeurs d'extensions

* La documentation décrit la version générale, la vérification de mise à jour par LibreOffice, le contrat des communications et le comportement en cas d'accès refusé.

### Sous le capot

* Un seul composant lit les numéros de version dans tout le service. Fiabilisation interne, tests supplémentaires.

## [0.9.18] - 2026-09-06

### Nouveautés

* À l'import d'une extension, sa version et son type se lisent d'abord dans son manifeste.

### Corrections

* Le service déclare lui-même sa version : l'écran d'administration n'affiche plus une valeur tenue à la main qui pouvait rester en retard.
* Un binaire d'extension servi depuis le cache est vérifié avant envoi : un fichier altéré n'est plus distribué.
* La session d'administration ne conserve plus qu'une référence légère ; un appel expiré ne crée plus de cookies inutiles.
* L'origine de la version détectée est correctement indiquée à l'import.
* La création automatique de la base et de ses rôles au démarrage devient optionnelle, à activer explicitement.
* Un message de télémétrie impossible à traiter est mis de côté sans arrêter le traitement des autres.
* Les mesures de télémétrie typées sont enregistrées avec leur type.
* L'export du parc retrouve une extension par son nom d'export comme par son identifiant, et signale une absence.

### Sous le capot

* Corrections de qualité de code ; tests supplémentaires sur la distribution des binaires et la sonde de concordance du parc.

## [0.9.17] - 2026-09-04

### Nouveautés

* Le service exporte périodiquement des agrégats d'usage du parc (nombre de postes, interactions) vers le tableau de bord de la bêta, avec un export manuel depuis l'écran d'administration.

### Corrections

* L'activité des appareils s'affiche de nouveau dans l'administration.

### Sous le capot

* Le contrat d'export du parc est verrouillé par des tests.

## [0.9.16] - 2026-08-30

### Nouveautés

* Le tableau de bord indique combien de versions différentes circulent réellement sur le parc.
* Le catalogue explique la cohabitation entre la version générale et les branches d'expérimentation : quelle version l'emporte, et pour qui.

### Corrections

* Une version marquée expérimentale n'apparaît plus comme la dernière version par erreur de tri.
* Un message de télémétrie impossible à traiter est mis de côté sans arrêter le traitement des autres.
* Diverses corrections issues de la revue de qualité.

### Sous le capot

* Documentation développeur référencée depuis l'accueil ; un test vérifie que l'API et les pages disent la même chose.

## [0.9.15] - 2026-07-26

### Corrections

* L'adresse d'une branche d'expérimentation est figée à la création ; la saisie assistée est unifiée.
* Corrections issues du second contrôle de qualité.

### Sous le capot

* La construction de l'image dans le cluster est fiabilisée et change d'outil.
* Tests étendus à la cohabitation de plusieurs branches d'expérimentation.

## [0.9.14] - 2026-07-25

### Nouveautés

* **Branches d'expérimentation** : plusieurs versions d'une extension peuvent coexister, chacune destinée à une cohorte de postes.
* Le service est prêt pour un hébergement infonuagique : stockage des fichiers en objet, observabilité, arrêt propre, et un paquet de déploiement documenté.
* L'image tourne sans privilèges et embarque ses migrations de base.

### Corrections

* Une campagne de mise à jour ne s'applique qu'à l'extension du poste qui la demande.
* La configuration livrée à un poste est choisie de façon déterministe, même en présence de doublons.
* Un refus d'accès à l'administration ne révèle plus quel groupe est requis.
* Le déploiement démarre correctement sur les plateformes qui exigent un utilisateur non privilégié.

## [0.9.12] - 2026-07-14

### Nouveautés

* Le tableau de bord montre l'historique du trafic vers le modèle de langage, et affiche la version du service avec une alerte quand plusieurs versions tournent en même temps.

## [0.9.11] - 2026-07-14

### Nouveautés

* Un drapeau de fonctionnalité peut être laissé au choix du poste, forcé activé ou forcé désactivé.

## [0.9.10] - 2026-07-14

### Corrections

* La suppression d'une extension entraîne celle de ses installations ; les drapeaux sont réconciliés sur tous les chemins d'import.

## [0.9.9] - 2026-07-14

### Corrections

* Le catalogue des drapeaux est réconcilié quel que soit le chemin d'import ; l'adresse de télémétrie remise aux postes est correcte derrière un préfixe d'URL.

### Sous le capot

* Décisions d'architecture documentées : frontière du service, choix du modèle de vectorisation, mode opératoire des drapeaux.

## [0.9.8] - 2026-07-14

### Corrections

* Le journal d'audit résout correctement une extension désignée par son numéro, et gère l'entrée « toutes les extensions ».

## [0.9.7] - 2026-07-14

### Nouveautés

* Le journal d'audit mémorise durablement l'extension concernée par chaque action.

## [0.9.6] - 2026-07-14

### Nouveautés

* Le journal d'audit gagne une colonne « Extension » avec un filtre à saisie assistée.

## [0.9.5] - 2026-07-14

### Nouveautés

* Journal d'audit plus dense : filtres instantanés avec saisie assistée, période, recherche dans les détails, défilement continu.

## [0.9.4] - 2026-07-14

### Nouveautés

* Tableau de bord : courbes par extension avec légende, tuiles cohérentes (appareils et interactions), bascule Appareils / Utilisateurs sur l'adoption.

### Corrections

* Chaque poste a une identité stable dans la télémétrie : plus de faux appareils.
* Une installation n'est enregistrée qu'avec sa version : plus d'installation fantôme.

## [0.9.3] - 2026-07-14

### Corrections

* Les installations sont enfin enregistrées ; un drapeau se crée pour une extension et une version données ; la page d'un drapeau fonctionne de nouveau.

## [0.9.2] - 2026-07-13

Première version suivie par ce journal.

### Nouveautés

* Les extensions obtiennent leur configuration auprès du service et peuvent la voir évoluer sans redémarrer : écran de configuration en direct (édition, comparaison, retour arrière), santé de chaque instance, secrets chiffrés.
* Drapeaux de fonctionnalité résolus côté service, par extension et par cohorte.
* Relais vers le modèle de langage compatible avec l'interface OpenAI, y compris la vectorisation de texte, avec journalisation des erreurs par les extensions.
* Les pages d'accueil et d'administration se rejoignent depuis la racine du service.
* Les journaux ne sont plus saturés par les sondes de santé et le rafraîchissement de l'administration.

### Corrections

* La connexion des extensions par le relais et la déconnexion de l'administration fonctionnent dans toutes les configurations de déploiement.
* Les adresses remises aux postes respectent le préfixe d'URL du déploiement.
* L'image tourne sans privilèges ; la base est protégée contre deux démarrages simultanés.

### Sous le capot

* Décisions d'architecture (réversibilité du fournisseur de modèle, frontières du service) documentées ; guide opérateur du relais ; harnais de développement local ; qualité du code : analyse de sécurité unifiée, corrections de style de code, tests bout en bout des drapeaux.
