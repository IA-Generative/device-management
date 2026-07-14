# Fix — `telemetryEndpoint` : servir un chemin relatif **à la racine** (double `/bootstrap`)

> **Pour l'agent de code de device-management.** Correctif **pérenne** du bug qui
> obligeait à contourner par config (`DM_TELEMETRY_PUBLIC_ENDPOINT` en URL absolue).
> Constaté sur DGX (ingress à préfixe `/bootstrap`) avec le plugin matisse ≥ 0.13.8.

## Symptôme
Les plugins postaient leurs traces sur **`/bootstrap/bootstrap/telemetry/v1/traces`**
→ **404**, traces jamais ingérées :
```
POST https://onyxia.gpu.minint.fr/bootstrap/bootstrap/telemetry/v1/traces → 404
[Telemetry] Flush error (non-auth, 404) — spans retained
```

## Cause racine
Le DM **et** le plugin ajoutent tous les deux le préfixe d'ingress `/bootstrap` :

1. **DM** — `app/main.py::_resolve_public_telemetry_endpoint()` : pour un endpoint
   **relatif** (`DM_TELEMETRY_PUBLIC_ENDPOINT=/telemetry/v1/traces`), il préfixe
   `PUBLIC_BASE_URL` **chemin compris** :
   ```python
   public_base = os.getenv("PUBLIC_BASE_URL").rstrip("/")   # = https://host/bootstrap
   return f"{public_base}{endpoint}"                        # → https://host/bootstrap/telemetry/v1/traces
   ```
2. **Plugin** — `telemetry.js::_resolveEndpoint()` : ne garde que le **PATH** du
   `telemetryEndpoint` reçu et le **re-base sur `bootstrapUrl`** (qui contient déjà
   `/bootstrap`) :
   ```js
   var path = String(endpoint).replace(/^https?:\/\/[^/]+/i, '');  // /bootstrap/telemetry/v1/traces
   return bootstrapUrl.replace(/\/+$/, '') + path;                  // .../bootstrap + /bootstrap/... → doublé
   ```

Résultat : `/bootstrap` compté deux fois. Le commentaire actuel de `_resolve_...`
(« préserver le CHEMIN de PUBLIC_BASE_URL … 502 ») date d'un **ancien** plugin qui ne
re-basait pas ; depuis que le plugin re-base systématiquement, cette préservation est
devenue la cause du **doublon**.

## Correctif demandé (pérenne, côté DM)
Un endpoint télémétrie **relatif** doit être servi **à la racine de l'origine**
(`scheme://netloc` **sans** le `BASE_PATH`), car c'est le **plugin** qui rajoute le
préfixe d'ingress en re-basant. Modifier `_resolve_public_telemetry_endpoint()` :

```python
def _resolve_public_telemetry_endpoint() -> str:
    endpoint = (settings.telemetry_public_endpoint or "").strip() or "/telemetry/v1/traces"
    if endpoint.startswith(("http://", "https://")):
        return endpoint                      # absolu explicite : inchangé
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if public_base:
        parsed = urlparse(public_base)
        if parsed.scheme and parsed.netloc:
            # RACINE de l'origine, SANS le BASE_PATH : le plugin (telemetry.js
            # _resolveEndpoint) re-base déjà le PATH sur bootstrapUrl (préfixe
            # d'ingress inclus). Préfixer BASE_PATH ici → double /bootstrap → 404.
            return f"{parsed.scheme}://{parsed.netloc}{endpoint}"
    return endpoint
```

Seule ligne qui change : `f"{public_base}{endpoint}"` → `f"{parsed.scheme}://{parsed.netloc}{endpoint}"`.

### Pourquoi c'est correct partout
- **DGX / prod-sdid** (BASE_PATH=`/bootstrap`) : sert `https://host/telemetry/v1/traces` ;
  le plugin re-base → `https://host/bootstrap/telemetry/v1/traces` ✓ (route `/bootstrap` → DM).
- **Intégration** (DM à la racine, pas de BASE_PATH) : `scheme://netloc` == `PUBLIC_BASE_URL`,
  donc **aucun changement** de comportement ✓.
- Un `DM_TELEMETRY_PUBLIC_ENDPOINT` **absolu** reste servi verbatim (échappatoire conservée).

## Conséquence déploiement (à répercuter dans `device-management-private`)
Une fois ce correctif livré, la **mitigation config peut être retirée** :
`DM_TELEMETRY_PUBLIC_ENDPOINT` peut **revenir au défaut relatif** `/telemetry/v1/traces`
(au lieu de l'URL absolue `https://<PUBLIC_HOSTNAME>/telemetry/v1/traces` posée en contournement).
Idéalement : livrer le code, déployer, **puis** simplifier `.env.config`.

## Critères d'acceptation
- Avec `DM_TELEMETRY_PUBLIC_ENDPOINT=/telemetry/v1/traces` et `PUBLIC_BASE_URL=https://host/bootstrap`,
  `config.json.telemetryEndpoint == https://host/telemetry/v1/traces` (racine, **sans** `/bootstrap`).
- Le plugin poste sur `https://host/bootstrap/telemetry/v1/traces` → **202** (plus de 404 doublé).
- Overlay sans BASE_PATH : `telemetryEndpoint` inchangé vs comportement actuel.
- Un endpoint configuré en absolu est renvoyé tel quel.
- (Bonus) Mettre à jour le commentaire obsolète de la fonction (le motif « 502 / préserver le
  chemin » ne s'applique plus au plugin re-baseur).

## Références
- DM : `app/main.py::_resolve_public_telemetry_endpoint()` (~ligne 810) ;
  `app/settings.py` (`TELEMETRY_PUBLIC_ENDPOINT`, env `DM_TELEMETRY_PUBLIC_ENDPOINT`).
- Plugin : `matisse/thunderbird/60.9.1/modules/telemetry.js::_resolveEndpoint()`.
- Contournement déploiement actuel : commit `5d3a1f1` de `device-management-private`
  (`.env.config` → `DM_TELEMETRY_PUBLIC_ENDPOINT=https://<PUBLIC_HOSTNAME>/telemetry/v1/traces`).
