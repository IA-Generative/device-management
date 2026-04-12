# Prompt — Deploy Wizard : Interface "Deploiement 1-2-3"

> Version : 1.0 — 2026-03-28
> Perimetre : device-management (admin UI)
> Stack : FastAPI + Jinja2 + HTMX + DSFR
> Prerequis : l'admin UI existante (`app/admin/`) reste inchangee. Le wizard est une **nouvelle page** ajoutee au routeur admin.

---

## Objectif

Remplacer le workflow actuel de creation de campagne (formulaire multi-champs complexe) par un **assistant lineaire en 3 etapes** qui guide l'administrateur du choix du plugin jusqu'au lancement d'un deploiement progressif automatique.

L'administrateur ne doit avoir besoin d'aucune connaissance technique sur les cohortes, les artifacts ou les campagnes. Il choisit, il configure, il lance — et il suit la progression en temps reel.

---

## Principes UX

1. **Zero jargon** : pas de "cohort", "artifact", "campaign" dans l'UI. Utiliser "groupe de test", "fichier plugin", "deploiement".
2. **Decisions minimales** : chaque etape = 1 decision principale + options secondaires cachees par defaut.
3. **Feedback visuel** : courbe de progression en temps reel avec paliers.
4. **Un seul bouton d'action par etape** : "Suivant", "Lancer", "Pause/Reprendre".

---

## Architecture technique

### Nouvelles routes (dans `app/admin/router.py`)

```
GET  /admin/deploy                    → Page wizard (etape 1 par defaut)
POST /admin/deploy/create             → Cree artifact + cohort + campaign en une seule requete
GET  /admin/deploy/{campaign_id}      → Page de suivi en temps reel
GET  /admin/api/deploy/{campaign_id}/progress  → Fragment HTMX : courbe + stats
POST /admin/deploy/{campaign_id}/pause         → Pause le deploiement
POST /admin/deploy/{campaign_id}/resume        → Reprend le deploiement
POST /admin/deploy/{campaign_id}/abort         → Annule + rollback
```

### Nouveau template

```
app/admin/templates/deploy_wizard.html     → Les 3 etapes + suivi
```

### Pas de nouveau modele DB

Le wizard reutilise les tables existantes (`campaigns`, `cohorts`, `artifacts`, `campaign_device_status`). La simplification est purement UI — le backend reste identique.

---

## Etape 1 — Choisir le plugin

### Interface

```
┌─────────────────────────────────────────────────────────┐
│  Deploiement 1-2-3                                      │
│  ━━━━━━━━━━━━━━━━━                                      │
│  ● Etape 1    ○ Etape 2    ○ Etape 3                   │
│                                                         │
│  Quel type de plugin ?                                  │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ LibreOff │  │ Firefox  │  │ Chrome   │              │
│  │  .oxt    │  │  .xpi    │  │  .crx    │              │
│  │  [ ● ]   │  │  [   ]   │  │  [   ]   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                         │
│  ┌──────────┐  ┌──────────┐                             │
│  │  Edge    │  │ Matisse  │                             │
│  │  .crx    │  │  .exe    │                             │
│  │  [   ]   │  │  [   ]   │                             │
│  └──────────┘  └──────────┘                             │
│                                                         │
│  Fichier du plugin :                                    │
│  ┌─────────────────────────────────────┐                │
│  │  [Choisir un fichier]  mirai-2.1.oxt│               │
│  └─────────────────────────────────────┘                │
│  Version : [ 2.1.0        ]                             │
│                                                         │
│                              [ Suivant → ]              │
└─────────────────────────────────────────────────────────┘
```

### Logique

- Les types de plugin sont derives de `DEVICE_ALLOWLIST` dans `main.py`.
- L'upload du fichier cree un `artifact` en base (via `artifacts.create_artifact`).
- La validation verifie l'extension (`.oxt`, `.xpi`, `.crx`) et calcule le SHA256.
- Le champ version est pre-rempli si le nom de fichier contient un pattern semver.

---

## Etape 2 — Definir la cible

### Interface

```
┌─────────────────────────────────────────────────────────┐
│  Deploiement 1-2-3                                      │
│  ━━━━━━━━━━━━━━━━━                                      │
│  ✓ Etape 1    ● Etape 2    ○ Etape 3                   │
│                                                         │
│  Qui recoit la mise a jour ?                            │
│                                                         │
│  (●) Tous les utilisateurs (deploiement progressif)     │
│  ( ) Un groupe de test d'abord                          │
│                                                         │
│  ┌ Si "groupe de test" ─────────────────────────────┐   │
│  │                                                   │   │
│  │  Choisir un groupe existant :                     │   │
│  │  [ Groupe beta-testeurs          ▼ ]              │   │
│  │                                                   │   │
│  │  — ou creer un groupe rapide —                    │   │
│  │                                                   │   │
│  │  ( ) Les N premiers pourcents : [ 10 ] %          │   │
│  │  ( ) Ces adresses email :                         │   │
│  │      ┌──────────────────────────────────┐         │   │
│  │      │ alice@example.com                │         │   │
│  │      │ bob@example.com                  │         │   │
│  │      └──────────────────────────────────┘         │   │
│  └───────────────────────────────────────────────────┘   │
│                                                         │
│                    [ ← Retour ]  [ Suivant → ]          │
└─────────────────────────────────────────────────────────┘
```

### Logique

- "Tous les utilisateurs" → `target_cohort_id = NULL` (cible globale).
- "Groupe de test" → selectionner un `cohort` existant ou en creer un inline (`percentage` ou `manual` avec emails).
- Le dropdown des groupes existants est charge via HTMX depuis `/admin/api/cohorts/list`.
- L'estimation du nombre de devices concernes s'affiche en temps reel (HTMX polling sur `/admin/api/cohorts/estimate`).

---

## Etape 3 — Configurer et lancer

### Interface

```
┌─────────────────────────────────────────────────────────┐
│  Deploiement 1-2-3                                      │
│  ━━━━━━━━━━━━━━━━━                                      │
│  ✓ Etape 1    ✓ Etape 2    ● Etape 3                   │
│                                                         │
│  Recapitulatif                                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Plugin : LibreOffice — mirai-2.1.oxt (v2.1.0)   │  │
│  │  Cible  : Tous les utilisateurs                   │  │
│  │  SHA256 : 8e032cea...b720b62                      │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  Type de deploiement :                                  │
│                                                         │
│  (●) Progressif (recommande)                            │
│      Paliers : 5% → 25% → 50% → 100%                  │
│      Duree entre paliers : [ 24 ] heures                │
│      Duree totale estimee : ~3 jours                    │
│                                                         │
│  ( ) Patch urgent                                       │
│      100% immediat, pas de paliers                      │
│                                                         │
│  ▸ Options avancees                                     │
│    Nom du deploiement : [ MaJ LibreOffice 2.1.0   ]    │
│    Fichier de rollback : [ Aucun            ▼ ]         │
│                                                         │
│           [ ← Retour ]  [ 🚀 Lancer le deploiement ]   │
└─────────────────────────────────────────────────────────┘
```

### Logique

- **Progressif** : genere un `rollout_config` avec 4 stages :
  ```json
  {
    "stages": [
      {"percent": 5,   "duration_hours": 24, "label": "Canary (5%)"},
      {"percent": 25,  "duration_hours": 24, "label": "Early adopters (25%)"},
      {"percent": 50,  "duration_hours": 24, "label": "Moitie (50%)"},
      {"percent": 100, "duration_hours": 0,  "label": "Deploiement complet"}
    ]
  }
  ```
- **Patch urgent** : `rollout_config = null`, `urgency = "critical"`, 100% immediat.
- La duree entre paliers est configurable (defaut 24h). La duree totale est calculee et affichee dynamiquement.
- Le nom du deploiement est auto-genere : `"MaJ {device_type} {version}"`.
- Le bouton "Lancer" fait un `POST /admin/deploy/create` qui :
  1. Cree l'artifact (si upload etape 1)
  2. Cree la cohort inline (si definie etape 2)
  3. Cree la campaign avec `status = 'active'` (lancement immediat)
  4. Redirige vers `/admin/deploy/{campaign_id}` (page de suivi)

---

## Page de suivi en temps reel

### Interface

```
┌─────────────────────────────────────────────────────────────────┐
│  MaJ LibreOffice 2.1.0                          [ ⏸ Pause ]    │
│  Lancee il y a 6h — Etape 2/4 : Early adopters (25%)          │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                                                           │  │
│  │  100% ┤                                          ╭───    │  │
│  │       │                                     ╭────╯       │  │
│  │   50% ┤                                ╭────╯            │  │
│  │       │                           ╭────╯                 │  │
│  │   25% ┤· · · · · · · · · · ·╭────╯· · · · · · · · · ·  │  │
│  │       │                 ╭────╯                            │  │
│  │    5% ┤· · · · · · ╭───╯ · · · · · · · · · · · · · · ·  │  │
│  │       │       ╭─────╯                                     │  │
│  │    0% ┤───────╯                                           │  │
│  │       └──┬──────┬──────┬──────┬──────┬──────┬──────┬──   │  │
│  │        J0     J0.5    J1    J1.5    J2    J2.5    J3      │  │
│  │                                                           │  │
│  │  ── Paliers prevus   ── Deploiement reel                  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  Cible   │  │  Mis a   │  │  En      │  │ Erreurs  │       │
│  │   1 200  │  │  jour    │  │ attente  │  │          │       │
│  │ devices  │  │   287    │  │   913    │  │    0     │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
│                                                                 │
│  Derniers evenements                                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 14:32  alice@mi..  v1.5→v2.1  ✓ Mis a jour              │  │
│  │ 14:31  bob@mini..  v1.5→v2.1  ✓ Mis a jour              │  │
│  │ 14:30  carol@mi..  —          ◷ Notifie                  │  │
│  │ 14:28  dave@min..  v1.5→v2.1  ✓ Mis a jour              │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  [ ⏸ Pause ]   [ ↩ Rollback ]   [ ✓ Terminer ]                │
└─────────────────────────────────────────────────────────────────┘
```

### Logique de la courbe

La courbe superpose **deux lignes** :

1. **Paliers prevus** (ligne en pointilles) : les seuils configures (5%, 25%, 50%, 100%) en escalier, avec l'axe X en temps base sur `duration_hours` de chaque stage.

2. **Deploiement reel** (ligne pleine) : pourcentage de devices effectivement mis a jour (`status='updated'` dans `campaign_device_status`) par rapport au total cible, echantillonne toutes les 5 minutes.

### Implementation de la courbe

- **Cote serveur** : le fragment HTMX `/admin/api/deploy/{campaign_id}/progress` retourne un JSON avec :
  ```json
  {
    "stages": [
      {"percent": 5,  "starts_at": "2026-03-28T10:00:00Z", "label": "Canary"},
      {"percent": 25, "starts_at": "2026-03-29T10:00:00Z", "label": "Early adopters"},
      ...
    ],
    "actual": [
      {"timestamp": "2026-03-28T10:05:00Z", "percent_updated": 0.2},
      {"timestamp": "2026-03-28T10:10:00Z", "percent_updated": 1.5},
      {"timestamp": "2026-03-28T10:15:00Z", "percent_updated": 3.8},
      ...
    ],
    "stats": {
      "total_target": 1200,
      "updated": 287,
      "pending": 913,
      "failed": 0,
      "notified": 45
    },
    "status": "active",
    "current_stage_label": "Early adopters (25%)",
    "current_stage_index": 1
  }
  ```

- **Cote client** : un `<canvas>` avec une lib JS legere inline (pas de build, pas de npm).
  Option recommandee : **Chart.js via CDN** (`<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js">`).
  Alternative zero-dependance : SVG genere cote serveur dans le template Jinja2.

- **Rafraichissement** : HTMX `hx-trigger="every 10s"` sur le fragment progress. Le canvas est re-rendu a chaque poll.

### Tuiles de metriques

Les 4 tuiles utilisent le meme pattern DSFR que le dashboard existant (`fr-tile`). Les valeurs viennent du meme endpoint `/progress`.

### Boutons d'action

| Bouton | Action | Condition |
|--------|--------|-----------|
| **Pause** | `POST .../pause` | status = active |
| **Reprendre** | `POST .../resume` | status = paused |
| **Rollback** | `POST .../abort` → status='rolled_back' | status = active ou paused |
| **Terminer** | `POST .../complete` → status='completed' | status = active et progression >= 95% |

Chaque action demande confirmation via une modale DSFR (`fr-modal`).

---

## Endpoint POST /admin/deploy/create

Ce endpoint orchestre les 3 etapes en une seule transaction :

```python
@router.post("/admin/deploy/create")
@require_admin
async def deploy_create(request: Request):
    form = await request.form()

    # 1. Upload artifact
    file = form["plugin_file"]
    device_type = form["device_type"]
    version = form["version"]
    data = await file.read()
    checksum = compute_checksum(data)
    # Save to /app/config/binaries/{device_type}/{filename} or S3
    artifact_id = create_artifact(cur, device_type, ..., version, path, checksum, ...)

    # 2. Cohort (optional)
    target_mode = form["target_mode"]  # "all" | "existing" | "percent" | "emails"
    cohort_id = None
    if target_mode == "existing":
        cohort_id = int(form["cohort_id"])
    elif target_mode == "percent":
        cohort_id = create_cohort(cur, name=f"auto-{version}-{pct}pct", type="percentage",
                                  config={"value": int(form["percent"])})
    elif target_mode == "emails":
        emails = [e.strip() for e in form["emails"].split("\n") if e.strip()]
        cohort_id = create_cohort(cur, name=f"auto-{version}-manual", type="manual", ...)
        add_members(cur, cohort_id, [("email", e) for e in emails])

    # 3. Campaign
    deploy_type = form["deploy_type"]  # "progressive" | "urgent"
    rollout_config = None
    urgency = "normal"
    if deploy_type == "progressive":
        hours = int(form.get("stage_hours", 24))
        rollout_config = {
            "stages": [
                {"percent": 5,   "duration_hours": hours, "label": "Canary (5%)"},
                {"percent": 25,  "duration_hours": hours, "label": "Early adopters (25%)"},
                {"percent": 50,  "duration_hours": hours, "label": "Moitie (50%)"},
                {"percent": 100, "duration_hours": 0,     "label": "Deploiement complet"},
            ]
        }
    else:
        urgency = "critical"

    name = form.get("name") or f"MaJ {device_type} {version}"
    campaign_id = create_campaign(
        cur, name=name, type="plugin_update",
        artifact_id=artifact_id,
        rollback_artifact_id=form.get("rollback_artifact_id") or None,
        target_cohort_id=cohort_id,
        urgency=urgency,
        status="active",  # lancement immediat
        rollout_config=rollout_config,
        created_by=request.state.admin_email,
    )

    return RedirectResponse(f"/admin/deploy/{campaign_id}", status_code=303)
```

---

## Integration avec l'admin existante

- Le wizard est accessible via un **gros bouton** "Nouveau deploiement" dans la sidebar et sur le dashboard.
- La page `/admin/campaigns` garde son fonctionnement actuel (vue detaillee pour les utilisateurs avances).
- Le lien "Voir les details avances" dans la page de suivi renvoie vers `/admin/campaigns/{id}`.
- Les deploiements crees par le wizard apparaissent normalement dans la liste des campagnes.

---

## Resume des fichiers a creer/modifier

| Fichier | Action |
|---------|--------|
| `app/admin/router.py` | Ajouter les 6 routes `/admin/deploy/*` |
| `app/admin/templates/deploy_wizard.html` | Nouveau template (wizard 3 etapes + suivi) |
| `app/admin/templates/base.html` | Ajouter lien "Nouveau deploiement" dans la sidebar |
| `app/admin/templates/dashboard.html` | Ajouter bouton "Nouveau deploiement" |
| `app/admin/static/dm-admin.css` | Styles pour la courbe, les etapes, les tuiles |

Aucune migration DB necessaire. Aucun nouveau service — reutilisation des services existants (`campaigns`, `cohorts`, `artifacts`).
