# Cahier de tests — Update & Feature Toggling

> Version : 1.0 — 2026-03-15
> Périmètre : EnrichedConfigResponse, campagnes de déploiement, feature flags
> Niveaux : Unitaire (U) · Intégration (I) · E2E (E) · Manuel utilisateur (M)

---

## 1. Matrice de couverture

| ID | Titre | Niveau | Composant | Statut |
|---|---|---|---|---|
| TC-DM-01 | Réponse sans X-Plugin-Version → update=null | U/I | DM | À valider |
| TC-DM-02 | Présence meta.schema_version=2 | U/I | DM | À valider |
| TC-DM-03 | Feature flag valeur par défaut | U/I | DM | À valider |
| TC-DM-04 | Override cohorte désactive un flag | U/I | DM | À valider |
| TC-DM-05 | min_plugin_version gate le flag | U/I | DM | À valider |
| TC-DM-06 | update.action=update quand plugin en retard | U/I | DM | À valider |
| TC-DM-07 | update=null quand plugin à jour | U/I | DM | À valider |
| TC-DM-08 | action=rollback quand plugin trop récent | U/I | DM | À valider |
| TC-DM-09 | campaign_device_status créé après fetch | U/I | DM | À valider |
| TC-DM-10 | Cohorte pourcentage stable | U/I | DM | À valider |
| TC-DM-11 | Variant TB60/TB128 déduit | U/I | DM | À valider |
| TC-DM-12 | Variant MV3 déduit | U/I | DM | À valider |
| TC-DM-13 | Campagne draft n'envoie pas d'update | U/I | DM | À valider |
| TC-DM-14 | config dict inchangé | U/I | DM | À valider |
| TC-DM-15 | Dégradation gracieuse sans tables migration | U/I | DM | À valider |
| TC-LO-01 | _is_feature_enabled sans cache → True | U | LO | À valider |
| TC-LO-02 | _is_feature_enabled cache False → False | U | LO | À valider |
| TC-LO-03 | _is_feature_enabled clé absente → défaut | U | LO | À valider |
| TC-LO-04 | fetch v2 popule _features_cache | U | LO | À valider |
| TC-LO-05 | fetch v2 schedule_update appelé | U | LO | À valider |
| TC-LO-06 | fetch v2 update=null → pas de schedule | U | LO | À valider |
| TC-LO-07 | fetch legacy ne touche pas features_cache | U | LO | À valider |
| TC-LO-08 | update non retriggeré si en cours | U | LO | À valider |
| TC-LO-09 | _perform_update checksum OK → install | U | LO | À valider |
| TC-LO-10 | _perform_update checksum KO → pas d'install | U | LO | À valider |
| TC-LO-11 | _perform_update libère flag sur exception | U | LO | À valider |
| TC-LO-12 | _get_extension_version retourne version | U | LO | À valider |
| TC-LO-13 | _get_extension_version retourne "" sur erreur | U | LO | À valider |
| TC-CR-01 | fetchDMConfig no-op sans dm_base_url | U | Chrome | À valider |
| TC-CR-02 | Headers corrects envoyés | U | Chrome | À valider |
| TC-CR-03 | Features stockées dans storage | U | Chrome | À valider |
| TC-CR-04 | handleUpdateDirective appelé sur action | U | Chrome | À valider |
| TC-CR-05 | Notification créée avec version | U | Chrome | À valider |
| TC-CR-06 | Message critique contient "Critique" | U | Chrome | À valider |
| TC-CR-07 | isFeatureEnabled défaut absent → true | U | Chrome | À valider |
| TC-CR-08 | isFeatureEnabled valeur stockée false → false | U | Chrome | À valider |
| TC-CR-09 | Erreur réseau silencieuse | U | Chrome | À valider |
| TC-CR-10 | Headers relay ajoutés si enrollé | U | Chrome | À valider |
| TC-TB-01 | Parsing v2 → champs update extraits | U | TB60 | À valider |
| TC-TB-02 | Parsing v2 update=null → _updateUrl null | U | TB60 | À valider |
| TC-TB-03 | Parsing v1 legacy → _updateUrl de la racine | U | TB60 | À valider |
| TC-TB-04 | Features extraites v2 | U | TB60 | À valider |
| TC-TB-05 | isFeatureEnabled clé false → false | U | TB60 | À valider |
| TC-TB-06 | isFeatureEnabled clé absente → défaut | U | TB60 | À valider |
| TC-TB-07 | XHR headers X-Plugin-Version envoyés | U | TB60 | À valider |
| TC-TB-08 | Checksum vérifié avant install | U | TB60 | À valider |
| TC-TB-09 | Install sans checksum → pas de vérif | U | TB60 | À valider |
| TC-TB-10 | Urgency critical → modal affiché | U | TB60 | À valider |
| TC-E2E-01 | /healthz répond 200 | E | DM | À valider |
| TC-E2E-02 | Config sans campagne → update=null | E | DM+LO | À valider |
| TC-E2E-03 | Cycle de vie complet campagne update | E | DM+LO | À valider |
| TC-E2E-04 | Campagne rollback | E | DM+LO | À valider |
| TC-E2E-05 | Feature flag par cohorte | E | DM | À valider |
| TC-E2E-06 | Téléchargement binaire + checksum | E | DM+S3 | À valider |
| TC-E2E-07 | Campagne pausée → update=null | E | DM | À valider |
| TC-M-01 | Notification update visible dans LO | M | LO | À valider |
| TC-M-02 | Dialog critique bloquant | M | LO | À valider |
| TC-M-03 | Notification navigateur Chrome | M | Chrome | À valider |
| TC-M-04 | Feature désactivée masque le menu | M | LO | À valider |
| TC-M-05 | Redémarrage LO après install | M | LO | À valider |

---

## 2. Cas de tests détaillés — niveau unitaire

### TC-DM-06 — update.action=update quand plugin en retard

**Préconditions**
- Table `artifacts` : version=`2.0.0`, device_type=`libreoffice`, is_active=true
- Table `campaigns` : status=`active`, type=`plugin_update`, artifact_id=celui ci-dessus

**Entrées**
```
GET /config/libreoffice/config.json
X-Plugin-Version: 1.0.0
X-Platform-Type: libreoffice
X-Client-UUID: test-uuid-dm06
```

**Sorties attendues**
```json
{
  "meta": { "schema_version": 2 },
  "update": {
    "action": "update",
    "current_version": "1.0.0",
    "target_version": "2.0.0",
    "artifact_url": "/binaries/libreoffice/2.0.0/mirai.oxt",
    "checksum": "sha256:<precomputed>",
    "urgency": "normal",
    "campaign_id": <id>
  }
}
```

**Vérifications post**
- `campaign_device_status` : ligne avec client_uuid=test-uuid-dm06, status=notified

**Critère de réussite** : HTTP 200, update.action == "update"

---

### TC-LO-09 — _perform_update checksum OK → install

**Préconditions**
- `_get_config_from_file("bootstrap_url")` = `http://localhost:9999`
- `_urlopen` mocké pour retourner `b"fake oxt"` (16 octets)
- checksum de la directive = sha256 calculé sur `b"fake oxt"`

**Appel**
```python
plugin._perform_update({
    "action": "update",
    "target_version": "2.0.0",
    "current_version": "1.0.0",
    "artifact_url": "/binaries/lo/2.0.0/mirai.oxt",
    "checksum": "sha256:" + sha256(b"fake oxt").hexdigest(),
    "urgency": "normal",
    "campaign_id": 1
})
```

**Vérifications**
- `mock_extension_manager.addExtension` appelé 1 fois
- `plugin._update_in_progress == False` après exécution
- Événement telemetry `ExtensionUpdated` envoyé avec `plugin.version_after="2.0.0"`

---

### TC-E2E-03 — Cycle de vie complet campagne update

**Scénario**

```
1. Insérer artifact en DB (version 2.0.0, lo)
2. Créer campagne active ciblant cet artifact
3. Plugin (simulé par httpx) GET /config/libreoffice/config.json
   → vérifier update.action=update
4. Plugin (simulé) GET /binaries/libreoffice/2.0.0/mirai.oxt
   → vérifier statut 200 et checksum binaire
5. Simuler telemetry ExtensionUpdated via POST /telemetry
6. Vérifier campaign_device_status.status = "notified"
   (updated mis à jour par le pipeline telemetry — optionnel en E2E v1)
```

**Durée estimée** : < 10 secondes (tout en mémoire + docker local)

---

## 3. Cas de tests manuels utilisateur

### TC-M-01 — Notification update visible dans LibreOffice

**Environnement** : LibreOffice 24.x avec plugin Mirai v1.x installé
**DM configuré** : campagne active ciblant v2.0.0

**Étapes**
1. Ouvrir LibreOffice Writer
2. Attendre jusqu'à 60 secondes (TTL cache config)
3. Observer la barre de statut / zone de notification LO

**Résultat attendu**
> Bandeau ou bulle : "Mirai 2.0.0 installé. Redémarrez LibreOffice pour finaliser."

**Résultat de rollback**
> Si urgency=critical et deadline dépassée : dialog modal bloquant avant installation

---

### TC-M-04 — Feature désactivée masque le menu

**Environnement** : DM avec feature_flag `calc_assistant` = false

**Étapes**
1. Ouvrir LibreOffice Calc
2. Observer le menu Mirai

**Résultat attendu**
> L'option "Assistant Calc" n'apparaît pas dans le menu (ou apparaît grisée)

---

## 4. Procédure d'exécution avant campagne

```
┌─────────────────────────────────────────────────────────────┐
│           CHECKLIST AVANT ACTIVATION DE CAMPAGNE            │
├─────────────────────────────────────────────────────────────┤
│ □ Tests unitaires DM passent (pytest tests/test_enriched_config.py)   │
│ □ Tests unitaires LO passent (pytest tests/test_update_features.py)   │
│ □ Tests unitaires Chrome passent (npm test chrome-extension/)          │
│ □ Tests unitaires TB60 passent (npm test matisse/thunderbird/60.9.1/)  │
│ □ Tests E2E passent (bash scripts/run-e2e.sh)                          │
│ □ Artifact uploadé en S3 / local avec checksum vérifié                │
│ □ Campagne créée en statut "draft" et vérifiée en DB                  │
│ □ Test manuel TC-M-01 validé sur un poste pilote                       │
│ □ Campagne activée sur cohorte 5% en premier                          │
│ □ Monitoring campaign_device_status pendant 30 min                    │
│ □ Taux d'erreur < 2% → élargir à 25%, puis 100%                       │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. Commandes d'exécution rapide

```bash
# Tests unitaires DM
cd device-management
pytest tests/test_enriched_config.py -v --tb=short

# Tests unitaires LO (dans AssistantMiraiLibreOffice)
cd AssistantMiraiLibreOffice
pytest tests/test_update_features.py -v --tb=short

# Tests unitaires Chrome
cd mirai-assistant/chrome-extension
npm test

# Tests unitaires TB60
cd mirai-assistant/matisse/thunderbird/60.9.1
npm test

# Suite E2E complète
cd device-management
bash scripts/run-e2e.sh

# Tout en une commande (CI)
make test-all
```

---

## 6. Rapport de test — template

```
Date           : YYYY-MM-DD
Campagne       : <nom>
Version cible  : <semver>
Exécutant      : <nom>

Tests unitaires DM    : PASS / FAIL  (<nb> cas, <nb> échecs)
Tests unitaires LO    : PASS / FAIL
Tests unitaires Chrome: PASS / FAIL
Tests unitaires TB60  : PASS / FAIL
Tests E2E             : PASS / FAIL  (<nb> cas, <nb> échecs)
Test manuel TC-M-01   : PASS / FAIL  (observé sur poste : <modèle>)

Décision : GO / NO-GO
Commentaires : ...
```
