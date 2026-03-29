# Integration du catalogue DM dans le portail MIrAI

## Description

Le fichier `mirai-catalog-snippet.html` contient un snippet HTML pret a coller dans le CMS Wagtail du portail MIrAI (mirai.interieur.gouv.fr). Il appelle l'API DM (`/catalog/api/plugins`) via Fetch et affiche les plugins sous forme de cartes DSFR responsives avec nom, description, icone, badges de maturite/version et nombre d'installations.

## Prerequis

- Les pages publiques du catalogue DM doivent etre deployees et accessibles :
  - `GET https://bootstrap.fake-domain.name/catalog/api/plugins` (API JSON)
  - `GET https://bootstrap.fake-domain.name/catalog/{slug}` (page de detail)
  - `GET https://bootstrap.fake-domain.name/catalog/{slug}/download` (telechargement)
- Le CORS doit etre configure sur DM (`Access-Control-Allow-Origin: *` -- deja en place).
- L'editeur doit disposer d'un acces administrateur au CMS Wagtail de Sites Faciles.

## Guide : coller le snippet dans Sites Faciles (Option B)

1. Se connecter a l'administration Wagtail de mirai.interieur.gouv.fr.
2. Dans l'arborescence, naviguer sous la section "Outils MIrAI".
3. Creer une nouvelle page enfant, titre : **"Extensions"**.
4. Dans le corps de la page (StreamField), ajouter les blocs suivants dans l'ordre :
   - **HeroBlock** : titre "Extensions pour vos outils bureautiques et navigateurs", texte d'introduction libre.
   - **RawHTMLBlock** : ouvrir le fichier `mirai-catalog-snippet.html`, copier l'integralite du contenu, et le coller dans le champ du bloc. Wagtail affiche un avertissement de securite -- c'est normal pour un RawHTMLBlock.
   - **AccordionBlock** : ajouter une FAQ d'installation par plateforme (LibreOffice, Thunderbird, Firefox).
5. Cliquer sur **Publier**.
6. Verifier le rendu sur la page publique. Les cartes doivent s'afficher apres un bref chargement. Si l'API DM est inaccessible, un message d'alerte jaune "Catalogue indisponible" s'affiche a la place.

## Les 3 options d'integration

| Option | Approche | Effort | Recommandation |
|--------|----------|--------|----------------|
| **A -- IframeBlock** | Embarquer `/catalog` de DM dans une iframe Wagtail | ~5 min | Depannage rapide. Double scrollbar possible, pas de navigation MIrAI autour, pas de SEO. |
| **B -- RawHTMLBlock** (recommande) | Coller le snippet Fetch API + rendu DSFR dans un RawHTMLBlock | ~30 min | Meilleur compromis : DSFR natif, navigation MIrAI preservee, fallback si DM est down, zero deploiement DM supplementaire. |
| **C -- App Django** | Creer une app `catalog_proxy` dans le projet Sites Faciles avec rendu serveur | ~2-4h | Evolution future si le portail devient un hub d'outils avec SEO complet. Necessite acces au repo Sites Faciles et un deploiement. |

## Fichiers

| Fichier | Usage |
|---------|-------|
| `mirai-catalog-snippet.html` | Snippet pret a coller dans le RawHTMLBlock (Option B) |
| `prompt-public-catalog-mirai.md` | Prompt complet decrivant les 3 options et le contexte technique |
