"""Clés d'API personnelles : appeler l'API du DM au nom de quelqu'un.

Jusqu'ici l'API n'avait qu'un jeton de service partagé (`DM_QUEUE_ADMIN_TOKEN`) :
un seul secret pour tout le monde, impossible à retirer à une personne, et un
journal d'audit qui ne dit pas qui a agi. Une clé d'API est créée par un
administrateur pour une personne ou un automate : elle porte un nom, une portée,
une échéance, se révoque seule, et chaque appel est attribué à son titulaire.

Forme : `dmk_<identifiant>_<secret>`. L'identifiant (public) retrouve la ligne ;
seul le SHA-256 du secret est stocké. Le secret est un aléa de 256 bits : un
hachage simple suffit, il n'y a rien à ralentir. La clé n'est montrée qu'une fois,
à sa création.

Portées :
- `read`  : lectures (requêtes GET) ;
- `write` : tout ce que fait le jeton de service (envoi de versions, campagnes).
Une clé peut être bornée à des plugins (`plugins`) : elle ne vaut alors que sur
les routes qui nomment l'un d'eux.

Module neutre : importable par app.main et app.admin sans cycle. Les fonctions
de base prennent un curseur ; la connexion et la transaction restent à l'appelant.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import secrets

PREFIX = "dmk_"
SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPES = (SCOPE_READ, SCOPE_WRITE)
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 366

_TOKEN_RE = re.compile(r"^dmk_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Écrire last_used_at à chaque appel coûterait une écriture par requête.
_TOUCH_EVERY_SECONDS = 60
_READ_METHODS = ("GET", "HEAD", "OPTIONS")

# Raisons de refus (stables : renvoyées par l'API et testées).
INVALID = "invalid_key"
REVOKED = "revoked_key"
EXPIRED = "expired_key"
SCOPE = "insufficient_scope"
PLUGIN = "plugin_not_allowed"

REASON_MESSAGES = {
    INVALID: "Clé d'API inconnue ou mal formée.",
    REVOKED: "Cette clé d'API a été révoquée.",
    EXPIRED: "Cette clé d'API a expiré : créez-en une nouvelle.",
    SCOPE: "Cette clé d'API n'a pas la portée requise pour cet appel.",
    PLUGIN: "Cette clé d'API ne vaut pas pour ce plugin.",
}


class ApiKeyInputError(ValueError):
    """Saisie refusée à la création d'une clé (message affichable)."""


def looks_like_key(token: str | None) -> bool:
    return bool(token) and str(token).startswith(PREFIX)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def token_from_headers(headers) -> str:
    """Clé présentée par l'appelant : `Authorization: Bearer dmk_…` ou `X-Admin-Token: dmk_…`."""
    auth = str(headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
        if looks_like_key(candidate):
            return candidate
    candidate = str(headers.get("x-admin-token") or "").strip()
    return candidate if looks_like_key(candidate) else ""


def _split(raw) -> list[str]:
    values = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[\s,]+", str(raw or ""))
    return [str(v).strip() for v in values if str(v).strip()]


def normalize_scopes(raw) -> list[str]:
    scopes = {s.lower() for s in _split(raw)}
    unknown = sorted(s for s in scopes if s not in SCOPES)
    if unknown:
        raise ApiKeyInputError(f"Portée inconnue : {', '.join(unknown)} (attendu : read, write).")
    if not scopes:
        raise ApiKeyInputError("Choisissez au moins une portée.")
    if SCOPE_WRITE in scopes:
        scopes.add(SCOPE_READ)   # qui écrit peut lire
    return sorted(scopes)


def normalize_plugins(raw) -> list[str]:
    plugins = sorted(set(_split(raw)))
    bad = [p for p in plugins if not _SLUG_RE.fullmatch(p)]
    if bad:
        raise ApiKeyInputError(f"Nom de plugin invalide : {', '.join(bad[:3])}.")
    return plugins


def expiry_in(ttl_days) -> datetime.datetime:
    """Échéance dans `ttl_days` jours à partir de maintenant. Lève ApiKeyInputError hors bornes."""
    try:
        days = int(ttl_days)
    except (TypeError, ValueError) as e:
        raise ApiKeyInputError("Durée de validité attendue en jours.") from e
    if not 1 <= days <= MAX_TTL_DAYS:
        raise ApiKeyInputError(
            f"Durée de validité : de 1 à {MAX_TTL_DAYS} jours. Une clé sans échéance n'est pas proposée.")
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)


def create(cur, *, label: str, owner_email: str, scopes, plugins=(), ttl_days: int | str = DEFAULT_TTL_DAYS,
           created_by: str = "") -> tuple[str, dict]:
    """Crée une clé → (clé en clair, à montrer UNE fois ; ligne créée, sans le secret)."""
    label = str(label or "").strip()
    owner_email = str(owner_email or "").strip().lower()
    if not label or len(label) > 100:
        raise ApiKeyInputError("Donnez un nom à la clé (100 caractères au plus) : à quoi sert-elle ?")
    if not _EMAIL_RE.fullmatch(owner_email):
        raise ApiKeyInputError("Adresse du titulaire attendue : c'est à elle que les appels seront attribués.")
    expires_at = expiry_in(ttl_days)
    scopes, plugins = normalize_scopes(scopes), normalize_plugins(plugins)

    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    cur.execute("""
        INSERT INTO api_keys (key_id, secret_hash, label, owner_email, scopes, plugins, expires_at, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, key_id, label, owner_email, scopes, plugins, created_at, expires_at
    """, (key_id, _hash(secret), label, owner_email, scopes, plugins, expires_at, created_by or None))
    cols = [d[0] for d in cur.description]
    return f"{PREFIX}{key_id}_{secret}", dict(zip(cols, cur.fetchone(), strict=False))


def list_keys(cur) -> list[dict]:
    """Toutes les clés, les valides d'abord. Jamais le secret ni son empreinte."""
    cur.execute("""
        SELECT id, key_id, label, owner_email, scopes, plugins, created_at, created_by,
               expires_at, last_used_at, last_used_ip, renewed_at, renewed_by, revoked_at, revoked_by,
               (revoked_at IS NULL AND expires_at > now()) AS active
        FROM api_keys ORDER BY (revoked_at IS NULL AND expires_at > now()) DESC, created_at DESC
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def renew(cur, key_pk: int, *, ttl_days: int | str = DEFAULT_TTL_DAYS, rotate: bool = True,
          renewed_by: str = "") -> tuple[str | None, dict | None]:
    """Renouvelle une clé : nouvelle échéance, et nouveau secret si `rotate`.

    - `rotate=True` (défaut) : un nouveau secret est tiré, l'ancien cesse de
      fonctionner aussitôt. La clé garde son identifiant, son nom, son titulaire,
      sa portée et ses plugins. Rend (clé en clair, à montrer UNE fois ; ligne).
    - `rotate=False` : seule l'échéance change, le secret en service reste le
      même. Rend (None, ligne).

    Une clé expirée se renouvelle ; une clé révoquée, non (ApiKeyInputError) :
    révoquer est définitif. (None, None) si la clé est inconnue.
    """
    expires_at = expiry_in(ttl_days)
    cur.execute("SELECT key_id, revoked_at FROM api_keys WHERE id = %s FOR UPDATE", (key_pk,))
    found = cur.fetchone()
    if not found:
        return None, None
    key_id, revoked_at = found
    if revoked_at is not None:
        raise ApiKeyInputError("Cette clé est révoquée : elle ne se renouvelle pas. Créez-en une nouvelle.")
    secret = secrets.token_urlsafe(32) if rotate else None
    cur.execute("""
        UPDATE api_keys SET expires_at = %s, renewed_at = now(), renewed_by = %s,
                            secret_hash = COALESCE(%s, secret_hash)
        WHERE id = %s
        RETURNING id, key_id, label, owner_email, scopes, plugins, created_at, expires_at
    """, (expires_at, renewed_by or None, _hash(secret) if secret else None, key_pk))
    cols = [d[0] for d in cur.description]
    row = dict(zip(cols, cur.fetchone(), strict=False))
    return (f"{PREFIX}{key_id}_{secret}" if secret else None), row


def revoke(cur, key_pk: int, *, revoked_by: str = "") -> dict | None:
    """Révoque une clé (sans effet si elle l'est déjà). Rend la ligne, ou None si inconnue."""
    cur.execute("""
        UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()),
                            revoked_by = COALESCE(revoked_by, %s)
        WHERE id = %s RETURNING id, key_id, label, owner_email
    """, (revoked_by or None, key_pk))
    row = cur.fetchone()
    return dict(zip(("id", "key_id", "label", "owner_email"), row, strict=False)) if row else None


def _plugin_allowed(cur, named: str, plugins: list[str]) -> bool:
    """`named` (nom au catalogue ou alias) désigne-t-il un plugin de la liste de la clé ?"""
    if named in plugins:
        return True
    cur.execute("""
        SELECT 1 FROM plugins p LEFT JOIN plugin_aliases a ON a.plugin_id = p.id
        WHERE (p.slug = %s OR a.alias = %s)
          AND (p.slug = ANY(%s) OR EXISTS (SELECT 1 FROM plugin_aliases b
                                           WHERE b.plugin_id = p.id AND b.alias = ANY(%s)))
        LIMIT 1
    """, (named, named, plugins, plugins))
    return cur.fetchone() is not None


def authenticate(cur, token: str, *, method: str = "GET", plugin: str | None = None,
                 plugin_id: int | None = None, ip: str | None = None) -> tuple[dict | None, str]:
    """(identité, "") si la clé vaut pour cet appel, sinon (None, raison).

    `plugin` / `plugin_id` : le plugin que nomme la route, s'il y en a un. Une
    clé bornée à des plugins est refusée en écriture sur une route qui n'en
    nomme aucun.
    """
    m = _TOKEN_RE.fullmatch(str(token or "").strip())
    if not m:
        return None, INVALID
    key_id, secret = m.group(1), m.group(2)
    cur.execute("""
        SELECT id, secret_hash, label, owner_email, scopes, plugins, expires_at, revoked_at, last_used_at, now()
        FROM api_keys WHERE key_id = %s
    """, (key_id,))
    row = cur.fetchone()
    # Comparaison à temps constant, y compris quand l'identifiant est inconnu.
    stored = str(row[1]) if row else "0" * 64
    if not hmac.compare_digest(_hash(secret), stored) or not row:
        return None, INVALID
    pk, _stored, label, owner_email, scopes, plugins, expires_at, revoked_at, last_used_at, now = row
    if revoked_at is not None:
        return None, REVOKED
    if expires_at is None or expires_at <= now:
        return None, EXPIRED
    scopes, plugins = list(scopes or []), list(plugins or [])
    reading = str(method or "GET").upper() in _READ_METHODS
    if (SCOPE_READ if reading else SCOPE_WRITE) not in scopes:
        return None, SCOPE
    if plugins:
        named = plugin
        if named is None and plugin_id is not None:
            cur.execute("SELECT slug FROM plugins WHERE id = %s", (plugin_id,))
            found = cur.fetchone()
            named = found[0] if found else ""
        if named is None:
            if not reading:
                return None, PLUGIN
        elif not _plugin_allowed(cur, named, plugins):
            return None, PLUGIN
    if last_used_at is None or (now - last_used_at).total_seconds() >= _TOUCH_EVERY_SECONDS:
        cur.execute("UPDATE api_keys SET last_used_at = now(), last_used_ip = %s WHERE id = %s", (ip, pk))
    return {
        "sub": f"apikey:{key_id}",
        "email": owner_email,
        "name": f"{label} (clé d'API)",
        "api_key": key_id,
        "scopes": scopes,
        "plugins": plugins,
    }, ""
