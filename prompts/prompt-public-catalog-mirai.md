# Prompt — Integration du catalogue plugins dans le portail MIrAI (Sites Faciles)

> Version : 1.0 — 2026-03-29
> Perimetre : portail mirai.interieur.gouv.fr (Sites Faciles / Wagtail)
> Prerequis : pages publiques DM operationnelles (`/catalog`, `/catalog/{slug}`, `/catalog/{slug}/download`)
> Dependance : prompt-public-catalog-dm.md (doit etre implemente en premier)

---

## Contexte

### Portail MIrAI

Le portail MIrAI (`mirai.interieur.gouv.fr`) est le site vitrine des outils IA du Ministere de l'Interieur. Il presente :
- Un header avec navigation (Outils MIrAI, Actualites, FAQ, A propos)
- Une grille de 7 cartes pour les outils existants (Chat, Resumer, Compte-Rendu, Prompts, RAG, OCR, Espace travail)
- Hero banner : "24 000 agents utilisent MIrAI, +3 000 nouveaux/mois"
- Design DSFR complet (police Marianne, couleurs gouvernementales, composants standards)

### CMS : Sites Faciles

Le portail est construit avec **Sites Faciles** (`github.com/numerique-gouv/sites-faciles`), un CMS gouvernemental :

- **Stack** : Django 6.0+ / Wagtail CMS / PostgreSQL
- **Design** : DSFR natif via `django-dsfr`
- **~40 blocs StreamField** pour construire les pages, organises en 5 categories

### Blocs pertinents disponibles

**Syntaxe experte** (cle pour l'integration) :

| Bloc | Description | Usage catalogue |
|------|-------------|----------------|
| **`RawHTMLBlock`** | HTML libre (readonly, avertissement securite) | Injecter le snippet Fetch API + rendu DSFR |
| **`IframeBlock`** | Iframe integree | Embarquer `/catalog` de DM directement |
| **`MarkdownBlock`** | Contenu Markdown | Documentation statique autour du catalogue |

**Composants DSFR natifs** :

| Bloc | Usage catalogue |
|------|----------------|
| **`CardBlock`** | Cartes plugin (image, titre, description, lien, badge) |
| **`BadgeBlock`** | Badges maturite/version |
| **`CalloutBlock`** | Encart d'appel a l'action (telechargement) |
| **`HeroBlock`** | Banniere hero "Extensions pour vos outils" |
| **`AccordionBlock`** | FAQ / instructions d'installation par plateforme |

### API DM disponible

L'API est deja operationnelle et retourne toutes les donnees necessaires :

```json
GET https://bootstrap.fake-domain.name/catalog/api/plugins
{
  "plugins": [{
    "slug": "mirai-libreoffice",
    "name": "MIrAI — IA'ssistant LibreOffice",
    "intent": "Assister les redacteurs...",
    "icon_url": "https://bootstrap.fake-domain.name/catalog/api/plugins/mirai-libreoffice/icon.png",
    "latest_version": "0.2.1",
    "maturity_label": "Stable",
    "key_features": ["Generer la suite", "Modifier", "Resumer", ...],
    "install_count": 245,
    "detail_url": "https://bootstrap.fake-domain.name/catalog/mirai-libreoffice",
    "download_url": "https://bootstrap.fake-domain.name/catalog/mirai-libreoffice/download"
  }],
  "total": 1,
  "generated_at": "2026-03-29T15:15:53Z"
}
```

Headers configures : `Access-Control-Allow-Origin: *`, `Cache-Control: public, max-age=300`.

---

## 3 approches evaluees

### Option A — `IframeBlock` (effort minimal)

Dans l'editeur Wagtail, creer une page enfant sous "Outils MIrAI" :
1. Titre : "Extensions"
2. Ajouter un bloc `IframeBlock`
3. URL : `https://bootstrap.fake-domain.name/catalog`
4. Hauteur : 80vh

| Critere | Evaluation |
|---------|-----------|
| **Effort** | ~5 min (config editeur Wagtail) |
| **Pro** | Zero code, deploiement immediat, DM evolue → l'iframe suit |
| **Con** | Pas de navigation MIrAI autour, double scrollbar possible, style legerement different |
| **Maintenance** | Aucune |
| **SEO** | Non (contenu dans iframe non indexe) |

### Option B — `RawHTMLBlock` + Fetch API (recommande)

Dans l'editeur Wagtail, creer une page "Extensions" et ajouter :
1. Un bloc `HeroBlock` : "Extensions pour vos outils bureautiques et navigateurs"
2. Un bloc `RawHTMLBlock` avec le snippet ci-dessous
3. Un bloc `AccordionBlock` : FAQ installation (LibreOffice, Thunderbird, Firefox)

**Snippet a coller dans le `RawHTMLBlock`** :

```html
<div id="plugin-catalog" class="fr-container fr-my-4w">
  <div class="fr-grid-row fr-grid-row--gutters" id="plugin-grid">
    <div class="fr-col-12">
      <p class="fr-text--sm fr-text--alt">Chargement du catalogue...</p>
    </div>
  </div>
</div>

<script>
fetch('https://bootstrap.fake-domain.name/catalog/api/plugins')
  .then(r => r.json())
  .then(data => {
    const grid = document.getElementById('plugin-grid');
    if (!data.plugins || !data.plugins.length) {
      grid.innerHTML = '<div class="fr-col-12"><p>Aucun plugin disponible.</p></div>';
      return;
    }
    const maturityColors = {
      'Stable':'fr-badge--success', 'Pre-release':'fr-badge--info',
      'Beta':'fr-badge--warning', 'Alpha':'fr-badge--new', 'Dev':'fr-badge--error'
    };
    grid.innerHTML = data.plugins.map(p => `
      <div class="fr-col-12 fr-col-md-4">
        <div class="fr-card fr-enlarge-link">
          <div class="fr-card__body">
            <div class="fr-card__content">
              <h3 class="fr-card__title">
                <a href="${p.detail_url}" class="fr-card__link">${p.name}</a>
              </h3>
              <p class="fr-card__desc">${p.intent || ''}</p>
              <div class="fr-card__end">
                <ul class="fr-badges-group">
                  <li><p class="fr-badge fr-badge--sm ${maturityColors[p.maturity_label] || ''}">${p.maturity_label}</p></li>
                  ${p.latest_version ? '<li><p class="fr-badge fr-badge--sm fr-badge--info">v' + p.latest_version + '</p></li>' : ''}
                  <li><p class="fr-badge fr-badge--sm fr-badge--no-icon">${p.install_count} installs</p></li>
                </ul>
              </div>
            </div>
          </div>
          ${p.icon_url ? `<div class="fr-card__header">
            <div class="fr-card__img">
              <img class="fr-responsive-img" src="${p.icon_url}" alt="${p.name}"
                   style="max-height:80px;object-fit:contain;">
            </div>
          </div>` : ''}
        </div>
      </div>
    `).join('');
  })
  .catch(() => {
    document.getElementById('plugin-grid').innerHTML =
      '<div class="fr-col-12"><div class="fr-alert fr-alert--warning"><p class="fr-alert__title">Catalogue indisponible</p><p>Le catalogue de plugins est temporairement indisponible. Reessayez dans quelques instants.</p></div></div>';
  });
</script>
```

| Critere | Evaluation |
|---------|-----------|
| **Effort** | ~30 min (creer page Wagtail + hero + coller snippet + FAQ) |
| **Pro** | DSFR natif (cartes, badges, grille responsive), navigation MIrAI preservee, fallback si DM down, zero deploiement DM |
| **Con** | `RawHTMLBlock` en readonly (modifiable par admin CMS uniquement), avertissement securite dans l'editeur |
| **Maintenance** | Snippet a mettre a jour si l'API DM evolue (rare — les champs sont stables) |
| **SEO** | Partiel (contenu genere en JS, titre/description statiques OK) |

### Option C — Application Django custom (integration profonde)

Creer une app Django `catalog_proxy` dans le projet Sites Faciles :

```python
# catalog_proxy/views.py
import requests
from django.shortcuts import render
from django.views.decorators.cache import cache_page

@cache_page(300)  # 5 min
def catalog_index(request):
    try:
        resp = requests.get(
            "https://bootstrap.fake-domain.name/catalog/api/plugins",
            timeout=5,
        )
        resp.raise_for_status()
        plugins = resp.json().get("plugins", [])
    except Exception:
        plugins = []
    return render(request, "catalog_proxy/index.html", {"plugins": plugins})

@cache_page(300)
def catalog_detail(request, slug):
    try:
        resp = requests.get(
            f"https://bootstrap.fake-domain.name/catalog/api/plugins/{slug}",
            timeout=5,
        )
        resp.raise_for_status()
        plugin = resp.json()
    except Exception:
        plugin = None
    if not plugin:
        from django.http import Http404
        raise Http404
    return render(request, "catalog_proxy/detail.html", {"plugin": plugin})
```

```python
# catalog_proxy/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("extensions/", views.catalog_index, name="catalog_index"),
    path("extensions/<slug:slug>/", views.catalog_detail, name="catalog_detail"),
]
```

```html
<!-- catalog_proxy/templates/catalog_proxy/index.html -->
{% extends "base.html" %}
{% load dsfr_tags %}

{% block content %}
<div class="fr-container fr-my-4w">
  <h1 class="fr-h2">Extensions pour vos outils</h1>
  <div class="fr-grid-row fr-grid-row--gutters">
    {% for p in plugins %}
    <div class="fr-col-12 fr-col-md-4">
      {% dsfr_card title=p.name description=p.intent link=p.detail_url image_url=p.icon_url %}
    </div>
    {% empty %}
    <div class="fr-col-12">
      {% dsfr_alert type="info" title="Catalogue vide" description="Aucun plugin disponible pour le moment." %}
    </div>
    {% endfor %}
  </div>
</div>
{% endblock %}
```

| Critere | Evaluation |
|---------|-----------|
| **Effort** | ~2-4h (app Django, templates, urls, deploiement Sites Faciles) |
| **Pro** | Rendu DSFR cote serveur (SEO complet), pas de JS client, pas de CORS, cache Django, template tags `dsfr_tags` natifs |
| **Con** | Necessite acces au repo Sites Faciles + deploiement, couplage plus fort |
| **Maintenance** | Templates a maintenir, mais logique simple (fetch + rendu) |
| **SEO** | Complet (HTML statique cote serveur) |

---

## Recommandation

```
┌─────────────────────────────────────────────────────────────────┐
│  Prerequis                                                       │
│  Implementer prompt-public-catalog-dm.md d'abord                │
│  → Pages /catalog dans DM, fonctionnent independamment           │
├─────────────────────────────────────────────────────────────────┤
│  Recommande : Option B — RawHTMLBlock                            │
│  → 30 min, DSFR natif, snippet pret a coller                    │
│  → Pas de deploiement cote DM, tout se passe dans Wagtail       │
│  → Le lien "Voir la fiche" pointe vers /catalog/{slug} sur DM  │
├─────────────────────────────────────────────────────────────────┤
│  Alternative immediate : Option A — IframeBlock                  │
│  → 5 min si besoin urgent                                        │
├─────────────────────────────────────────────────────────────────┤
│  Evolution future : Option C — App Django                        │
│  → Si le portail MIrAI evolue vers un hub d'outils avec         │
│    navigation integree et SEO important                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## Checklist d'implementation (Option B)

- [ ] Acces editeur au CMS Wagtail de mirai.interieur.gouv.fr
- [ ] Creer une page enfant sous "Outils MIrAI" → titre "Extensions"
- [ ] Ajouter un bloc `HeroBlock` : titre + texte d'introduction
- [ ] Ajouter un bloc `RawHTMLBlock` : coller le snippet JS ci-dessus
- [ ] Ajouter un bloc `AccordionBlock` : FAQ installation par plateforme
- [ ] Verifier que le CORS fonctionne (l'API DM est deja configuree `*`)
- [ ] Tester le fallback (couper DM temporairement → le message d'erreur s'affiche)
- [ ] Publier la page
