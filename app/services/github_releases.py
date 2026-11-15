"""Relevé des releases GitHub d'un plugin (issue #108).

Un plugin du catalogue peut déclarer d'où viennent ses paquets
(``plugins.github_source``) : dépôt, composant release-please, motif de
l'artefact, canal suivi. Ce module :

- interroge ``GET /repos/{dépôt}/releases`` (pagination bornée) ;
- retient les releases du composant, lit la version dans le tag, les classe
  comme le reste du DM (``version_sort_key``), cherche l'artefact qui correspond
  au motif et son enveloppe ``<fichier>.sig.json`` (ADR-0006) ;
- range le résultat dans ``plugin_github_releases`` (état : disponible,
  importée, refusée, ignorée — avec une raison lisible) ;
- télécharge, à la demande de l'administration, l'artefact d'une release et sa
  signature. L'import lui-même reste celui de l'envoi manuel
  (``package_import.import_package``) : ce module ne stocke ni ne publie rien.

Le relevé périodique ne tourne que dans le worker (issue #105). Un DM sans accès
sortant (proxy refusé, délai dépassé, 401/403/404) garde l'état et le message de
l'échec dans ``plugin_github_state``, espace ses essais et n'écrit au journal
qu'aux changements d'état.

Le jeton (``DM_GITHUB_TOKEN``) ne vient que de l'environnement : jamais en base,
jamais dans une réponse, un message d'erreur ou un journal. Les messages d'erreur
sont composés ici, sans reprendre le texte des exceptions réseau (qui pourrait
porter l'adresse d'un proxy avec ses identifiants).

Module neutre : importable par app.main, app.worker_main et app.admin sans cycle.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from . import package_signing
from .db import get_db_connection
from .versions import NATURE_CANDIDATE, NATURE_GENERAL, nature_label, version_nature, version_sort_key

logger = logging.getLogger("dm-github-releases")

# ── Source déclarée par plugin ───────────────────────────────────────────────
CHANNEL_GENERAL = "general"        # versions X.Y.Z seulement
CHANNEL_RC = "general+rc"          # X.Y.Z et X.Y.Z-rc.N
CHANNEL_ALL = "all"                # tout, essais (-dev.N) compris
CHANNELS = (CHANNEL_GENERAL, CHANNEL_RC, CHANNEL_ALL)
CHANNEL_LABELS = {
    CHANNEL_GENERAL: "versions générales seulement",
    CHANNEL_RC: "versions générales et candidates",
    CHANNEL_ALL: "toutes les versions",
}

# ── États d'une release relevée ──────────────────────────────────────────────
STATE_AVAILABLE = "available"
STATE_IMPORTED = "imported"
STATE_REFUSED = "refused"
STATE_IGNORED = "ignored"

# ── Codes d'échec du relevé (stables : rangés en base, affichés, testés) ─────
ERR_NETWORK = "network"
ERR_TIMEOUT = "timeout"
ERR_PROXY = "proxy"
ERR_UNAUTHORIZED = "unauthorized"
ERR_FORBIDDEN = "forbidden"
ERR_RATE_LIMITED = "rate_limited"
ERR_NOT_FOUND = "not_found"
ERR_HTTP = "http_error"
ERR_INVALID = "invalid_response"
ERR_TOO_LARGE = "too_large"
ERR_SOURCE = "source"
ERR_INTERNAL = "internal"

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_POLL_INTERVAL = 900

_PER_PAGE = 100
_MAX_PAGES = 3                      # 300 releases : les plus récentes d'abord
_MAX_ROWS = 60                      # lignes gardées par plugin, les plus récentes
_MAX_NOTES = 20_000
_MAX_SIGNATURE_SIZE = 256 * 1024
DEFAULT_MAX_ASSET_SIZE = 100 * 1024 * 1024
_BACKOFF_MAX = 6 * 3600
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

_REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_PATTERN_RE = re.compile(r"[A-Za-z0-9._+*?\[\]!-]{1,200}")
_NAME_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?)\.[A-Za-z0-9]+$")


class SourceError(ValueError):
    """Source GitHub mal formée (message destiné à l'administrateur)."""


class GithubError(Exception):
    """Échec d'un appel à GitHub. `code` est l'un des ERR_* ; le message est affichable."""

    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


# ── Réglages (environnement seulement) ───────────────────────────────────────

def api_url() -> str:
    return (os.environ.get("DM_GITHUB_API_URL", "").strip() or DEFAULT_API_URL).rstrip("/")


def poll_interval() -> int:
    """Secondes entre deux relevés périodiques ; 0 = relevé périodique désactivé."""
    raw = os.environ.get("DM_GITHUB_POLL_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_POLL_INTERVAL


def _token() -> str:
    return os.environ.get("DM_GITHUB_TOKEN", "").strip()


def token_configured() -> bool:
    """Un jeton est-il fourni ? (Seule information sur le jeton qui sorte de ce module.)"""
    return bool(_token())


def normalize_source(raw) -> dict | None:
    """Source validée {repository, component, asset_pattern, channel}, ou None si absente.

    Lève SourceError avec un message affichable. Une adresse complète de dépôt
    (https://github.com/org/depot) est acceptée et réduite à « org/depot ».
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except ValueError as e:
            raise SourceError("Source GitHub illisible.") from e
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise SourceError("Source GitHub illisible.")
    repository = str(raw.get("repository") or "").strip()
    repository = re.sub(r"^https?://[^/]+/", "", repository).removesuffix(".git").strip("/")
    if not repository:
        return None
    if not _REPOSITORY_RE.fullmatch(repository):
        raise SourceError("Dépôt attendu sous la forme « organisation/depot ».")
    component = str(raw.get("component") or "").strip()
    if component and not _COMPONENT_RE.fullmatch(component):
        raise SourceError("Composant : lettres, chiffres, point, tiret et souligné seulement.")
    pattern = str(raw.get("asset_pattern") or "").strip()
    if not pattern:
        raise SourceError("Le motif de l'artefact est requis (ex. mirai-matisse-*.xpi).")
    if not _PATTERN_RE.fullmatch(pattern):
        raise SourceError("Motif de l'artefact : un nom de fichier avec * ou ?, sans chemin.")
    channel = str(raw.get("channel") or CHANNEL_GENERAL).strip()
    if channel not in CHANNELS:
        raise SourceError("Canal inconnu (attendu : general, general+rc ou all).")
    return {"repository": repository, "component": component,
            "asset_pattern": pattern, "channel": channel}


# ── Lecture des releases (pur, sans réseau ni base) ──────────────────────────

def tag_version(tag: str, component: str = "") -> str | None:
    """Version portée par un tag release-please, ou None si le tag n'est pas du composant.

    Composant « IAssistant-tb60 » : tags « IAssistant-tb60-vX.Y.Z ». Composant
    vide : tags « vX.Y.Z » (ou « X.Y.Z »). Un tag dont la version ne peut pas
    être ordonnée est écarté.
    """
    tag = str(tag or "").strip()
    if component:
        prefix = f"{component}-v"
        if not tag.startswith(prefix):
            return None
        version = tag[len(prefix):]
    else:
        version = tag[1:] if tag.startswith("v") else tag
    if not version or not version[0].isdigit() or version_sort_key(version) is None:
        return None
    return version


def in_channel(nature: str, channel: str) -> bool:
    if channel == CHANNEL_ALL:
        return True
    if channel == CHANNEL_RC:
        return nature in (NATURE_GENERAL, NATURE_CANDIDATE)
    return nature == NATURE_GENERAL


def _pick_asset(assets: list, pattern: str, version: str) -> tuple[dict | None, str | None]:
    """(artefact retenu, raison s'il n'y en a pas) parmi les fichiers d'une release."""
    by_name = {a["name"]: a for a in assets
               if isinstance(a, dict) and isinstance(a.get("name"), str)}
    matches = sorted(n for n in by_name
                     if fnmatch.fnmatchcase(n, pattern) and not n.endswith(".sig.json"))
    if not matches:
        return None, f"Aucun fichier de la release ne correspond au motif « {pattern} »."
    if len(matches) > 1:
        # Le fichier nommé selon la convention <slug>-<version>.<ext> l'emporte.
        precise = [n for n in matches
                   if (m := _NAME_VERSION_RE.search(n)) and m.group(1) == version]
        if len(precise) != 1:
            return None, (f"Plusieurs fichiers de la release correspondent au motif « {pattern} » "
                          f"({', '.join(matches[:5])}) : précisez le motif.")
        matches = precise
    return by_name[matches[0]], None


def _api_asset_url(asset: dict | None) -> str | None:
    url = (asset or {}).get("url")
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def _iso_date(value) -> str | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except (TypeError, ValueError):
        return None


def classify(releases: list, source: dict, known_versions=()) -> list[dict]:
    """Lignes du relevé pour `source`, de la version la plus récente à la plus ancienne.

    `known_versions` : versions déjà au catalogue (→ « importée »). Les brouillons
    et les tags d'un autre composant sont écartés. Le reste est classé :
    hors canal → ignorée ; sans artefact → ignorée ; sinon disponible. Les
    remarques (drapeau pré-version discordant, signature absente) vont dans
    `reason` sans changer l'état : c'est le numéro de version qui fait foi.
    """
    known = {str(v) for v in known_versions}
    channel = source.get("channel") or CHANNEL_GENERAL
    rows: dict[str, dict] = {}
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        version = tag_version(tag, source.get("component") or "")
        if version is None or tag in rows or len(tag) > 200 or len(version) > 100:
            continue
        nature = version_nature(version)
        flagged = bool(release.get("prerelease"))
        remarks: list[str] = []
        if flagged and nature == NATURE_GENERAL:
            remarks.append(f"GitHub marque cette release comme pré-version, mais son numéro {version} "
                           "est celui d'une version générale : c'est le numéro qui fait foi.")
        elif not flagged and nature != NATURE_GENERAL:
            remarks.append(f"Le numéro {version} est celui d'une pré-version, mais GitHub ne marque pas "
                           "la release comme telle : c'est le numéro qui fait foi.")

        assets = release.get("assets") if isinstance(release.get("assets"), list) else []
        asset, missing = _pick_asset(assets, source["asset_pattern"], version)
        signature = None
        if asset is not None:
            signature = next((a for a in assets if isinstance(a, dict)
                              and a.get("name") == asset["name"] + ".sig.json"), None)

        if version in known:
            state, remarks = STATE_IMPORTED, []
        elif not in_channel(nature, channel):
            state = STATE_IGNORED
            remarks.insert(0, f"Hors du canal suivi ({CHANNEL_LABELS[channel]}) : "
                              f"version {nature_label(nature).lower()}.")
        elif asset is None or _api_asset_url(asset) is None:
            state = STATE_IGNORED
            remarks.insert(0, missing or "L'artefact de la release n'a pas d'adresse de téléchargement.")
        else:
            state = STATE_AVAILABLE
            if _api_asset_url(signature) is None:
                note = f"Aucune enveloppe de signature ({asset['name']}.sig.json) n'accompagne ce paquet."
                if package_signing.policy() == package_signing.POLICY_REQUIRE:
                    note += " Cette instance l'exige : l'import sera refusé."
                remarks.append(note)

        html_url = release.get("html_url")
        rows[tag] = {
            "tag": tag, "version": version, "prerelease": flagged, "nature": nature,
            "asset_name": asset["name"] if asset else None,
            "asset_api_url": _api_asset_url(asset),
            "signature_api_url": _api_asset_url(signature),
            "release_url": html_url if isinstance(html_url, str) and html_url.startswith("http") else None,
            "release_notes": str(release.get("body") or "")[:_MAX_NOTES] or None,
            "published_at": _iso_date(release.get("published_at") or release.get("created_at")),
            "state": state, "reason": " ".join(remarks) or None,
        }
    ordered = sorted(rows.values(), key=lambda r: version_sort_key(r["version"]), reverse=True)
    return ordered[:_MAX_ROWS]


def backoff_delay(failures: int, interval: int | None = None, retry_after: int | None = None) -> int:
    """Secondes à attendre après `failures` échecs consécutifs (exponentiel, borné).

    Base : l'intervalle de relevé (au moins une minute). 1er échec : 2 × la base,
    puis le double à chaque échec, plafonné à six heures (ou à la base si elle
    est plus longue). `retry_after` (limite de débit annoncée par GitHub) allonge
    le délai sans dépasser le plafond.
    """
    base = max(60, int(interval or DEFAULT_POLL_INTERVAL))
    ceiling = max(_BACKOFF_MAX, base)
    delay = min(base * (2 ** min(max(1, int(failures)), 12)), ceiling)
    if retry_after and retry_after > 0:
        delay = max(delay, min(int(retry_after), ceiling))
    return delay


# ── Appels HTTP ──────────────────────────────────────────────────────────────

def _headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {"Accept": accept, "User-Agent": "device-management",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _client(transport=None, timeout: httpx.Timeout = _TIMEOUT) -> httpx.Client:
    """Client httpx : proxy de sortie lu dans l'environnement (HTTPS_PROXY…),
    redirections suivies (httpx retire Authorization en changeant d'origine)."""
    return httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=5, transport=transport)


def _transport_error(exc: Exception) -> GithubError:
    """Erreur réseau → GithubError, sans reprendre le texte de l'exception."""
    if isinstance(exc, httpx.TimeoutException):
        return GithubError(ERR_TIMEOUT, "GitHub n'a pas répondu dans le délai imparti.")
    if isinstance(exc, httpx.ProxyError):
        return GithubError(ERR_PROXY, "Le proxy de sortie a refusé la connexion vers GitHub.")
    if isinstance(exc, httpx.TooManyRedirects):
        return GithubError(ERR_HTTP, "GitHub a répondu par trop de redirections.")
    return GithubError(ERR_NETWORK, f"GitHub est injoignable depuis ce DM ({type(exc).__name__}).")


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after", "")
    if raw.isdigit():
        return int(raw)
    reset = response.headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return max(0, int(reset) - int(time.time()))
    return None


def _check_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    if status == 401:
        raise GithubError(ERR_UNAUTHORIZED,
                          "GitHub refuse le jeton d'accès (401) : vérifiez DM_GITHUB_TOKEN.")
    if status in (403, 429):
        if status == 429 or response.headers.get("x-ratelimit-remaining") == "0":
            hint = "" if token_configured() else " Un jeton (DM_GITHUB_TOKEN) relève cette limite."
            raise GithubError(ERR_RATE_LIMITED,
                              f"Limite de débit de GitHub atteinte ({status}).{hint}",
                              retry_after=_retry_after(response))
        raise GithubError(ERR_FORBIDDEN,
                          "Accès refusé (403) : dépôt privé sans jeton, jeton sans droit de lecture, "
                          "ou destination interdite par le proxy de sortie.")
    if status == 404:
        raise GithubError(ERR_NOT_FOUND,
                          "Dépôt ou fichier introuvable (404) : nom erroné, ou dépôt privé sans jeton.")
    if status == 407:
        raise GithubError(ERR_PROXY, "Le proxy de sortie demande une authentification (407).")
    raise GithubError(ERR_HTTP, f"GitHub a répondu par une erreur ({status}).")


def fetch_releases(repository: str, *, client: httpx.Client | None = None) -> list:
    """Releases du dépôt, les plus récentes d'abord (au plus _MAX_PAGES pages). Lève GithubError."""
    own = client is None
    client = client or _client()
    releases: list = []
    try:
        for page in range(1, _MAX_PAGES + 1):
            try:
                response = client.get(f"{api_url()}/repos/{repository}/releases",
                                      params={"per_page": _PER_PAGE, "page": page},
                                      headers=_headers())
            except httpx.HTTPError as exc:
                raise _transport_error(exc) from None
            _check_status(response)
            try:
                batch = response.json()
            except ValueError:
                batch = None
            if not isinstance(batch, list):
                raise GithubError(ERR_INVALID, "Réponse de GitHub illisible (liste de releases attendue).")
            releases.extend(batch)
            # La suite est annoncée par l'en-tête Link ; sans lui, une page pleine en appelle une autre.
            more = "next" in response.links if response.headers.get("link") else len(batch) >= _PER_PAGE
            if not batch or not more:
                break
    finally:
        if own:
            client.close()
    return releases


def _same_origin(url: str, base: str) -> bool:
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


def _download(client: httpx.Client, url: str, max_size: int) -> bytes:
    # L'adresse vient du relevé : elle doit être celle de l'API configurée, pour
    # que le jeton ne parte jamais vers un autre hôte.
    if not url or not _same_origin(url, api_url()):
        raise GithubError(ERR_INVALID, "L'adresse de l'artefact n'appartient pas à l'API GitHub configurée.")
    try:
        with client.stream("GET", url, headers=_headers("application/octet-stream")) as response:
            _check_status(response)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_size:
                raise GithubError(ERR_TOO_LARGE, _too_large(max_size))
            content = bytearray()
            for chunk in response.iter_bytes():
                content += chunk
                if len(content) > max_size:
                    raise GithubError(ERR_TOO_LARGE, _too_large(max_size))
    except httpx.HTTPError as exc:
        raise _transport_error(exc) from None
    return bytes(content)


def _too_large(max_size: int) -> str:
    return f"Fichier trop volumineux (plus de {max_size // (1024 * 1024) or 1} Mo)."


def download_release(row: dict, *, max_size: int = DEFAULT_MAX_ASSET_SIZE,
                     client: httpx.Client | None = None) -> tuple[bytes, str | None]:
    """(octets de l'artefact, texte de son enveloppe .sig.json ou None). Lève GithubError.

    Les octets sont rendus tels que publiés : rien n'est réécrit ici.
    """
    own = client is None
    client = client or _client(timeout=_DOWNLOAD_TIMEOUT)
    try:
        data = _download(client, row.get("asset_api_url") or "", max_size)
        signature = None
        if row.get("signature_api_url"):
            raw = _download(client, row["signature_api_url"], _MAX_SIGNATURE_SIZE)
            signature = raw.decode("utf-8-sig", errors="replace")
    finally:
        if own:
            client.close()
    if not data:
        raise GithubError(ERR_INVALID, "L'artefact téléchargé est vide.")
    return data, signature


# ── Base : relevé et état ────────────────────────────────────────────────────

_ROW_COLUMNS = ("tag", "version", "prerelease", "nature", "asset_name", "asset_api_url",
                "signature_api_url", "release_url", "release_notes", "published_at", "state", "reason")
_STATE_COLUMNS = ("checked_at", "succeeded_at", "status", "error_code", "message",
                  "failures", "next_attempt_at", "requested_at")


def known_versions(cur, plugin_id: int) -> set[str]:
    cur.execute("SELECT version FROM plugin_versions WHERE plugin_id = %s", (plugin_id,))
    return {row[0] for row in cur.fetchall()}


def store_releases(cur, plugin_id: int, rows: list[dict]) -> None:
    """Remplace le relevé du plugin par `rows`. Un refus d'import déjà noté est gardé
    tant que la release publie le même fichier (on ne le « redécouvre » pas disponible)."""
    cur.execute("SELECT tag, state, reason, asset_name FROM plugin_github_releases WHERE plugin_id = %s",
                (plugin_id,))
    previous = {tag: (state, reason, asset) for tag, state, reason, asset in cur.fetchall()}
    for row in rows:
        row = dict(row)
        before = previous.get(row["tag"])
        if (before and before[0] == STATE_REFUSED and row["state"] == STATE_AVAILABLE
                and before[2] == row["asset_name"]):
            row["state"], row["reason"] = STATE_REFUSED, before[1]
        cur.execute(f"""
            INSERT INTO plugin_github_releases (plugin_id, {', '.join(_ROW_COLUMNS)}, checked_at)
            VALUES (%s, {', '.join(['%s'] * len(_ROW_COLUMNS))}, now())
            ON CONFLICT (plugin_id, tag) DO UPDATE SET
                {', '.join(f'{c} = EXCLUDED.{c}' for c in _ROW_COLUMNS if c != 'tag')},
                checked_at = now()
        """, (plugin_id, *[row.get(c) for c in _ROW_COLUMNS]))
    cur.execute("DELETE FROM plugin_github_releases WHERE plugin_id = %s AND NOT (tag = ANY(%s))",
                (plugin_id, [row["tag"] for row in rows]))


def list_releases(cur, plugin_id: int) -> list[dict]:
    cur.execute(f"SELECT {', '.join(_ROW_COLUMNS)}, checked_at FROM plugin_github_releases "
                "WHERE plugin_id = %s", (plugin_id,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    rows.sort(key=lambda r: version_sort_key(r["version"]) or ((0,), 0, ()), reverse=True)
    return rows


def get_release(cur, plugin_id: int, tag: str) -> dict | None:
    cur.execute(f"SELECT {', '.join(_ROW_COLUMNS)} FROM plugin_github_releases "
                "WHERE plugin_id = %s AND tag = %s", (plugin_id, tag))
    row = cur.fetchone()
    return dict(zip(_ROW_COLUMNS, row, strict=False)) if row else None


def mark_release(cur, plugin_id: int, tag: str, state: str, reason: str | None = None) -> None:
    """Note le sort d'un import (importée, refusée avec sa raison) sur la ligne du relevé."""
    cur.execute("UPDATE plugin_github_releases SET state = %s, reason = %s "
                "WHERE plugin_id = %s AND tag = %s", (state, (reason or None), plugin_id, tag))


def get_state(cur, plugin_id: int) -> dict | None:
    cur.execute(f"SELECT {', '.join(_STATE_COLUMNS)} FROM plugin_github_state WHERE plugin_id = %s",
                (plugin_id,))
    row = cur.fetchone()
    return dict(zip(_STATE_COLUMNS, row, strict=False)) if row else None


def _record_success(cur, plugin_id: int) -> None:
    interval = poll_interval()
    cur.execute("""
        INSERT INTO plugin_github_state (plugin_id, checked_at, succeeded_at, status, failures, next_attempt_at)
        VALUES (%(id)s, now(), now(), 'ok', 0, now() + make_interval(secs => %(wait)s))
        ON CONFLICT (plugin_id) DO UPDATE SET
            checked_at = now(), succeeded_at = now(), status = 'ok', error_code = NULL, message = NULL,
            failures = 0, next_attempt_at = now() + make_interval(secs => %(wait)s), requested_at = NULL
    """, {"id": plugin_id, "wait": interval or DEFAULT_POLL_INTERVAL})


def _record_failure(cur, plugin_id: int, error: GithubError) -> None:
    cur.execute("SELECT failures FROM plugin_github_state WHERE plugin_id = %s", (plugin_id,))
    row = cur.fetchone()
    failures = (row[0] if row else 0) + 1
    wait = backoff_delay(failures, poll_interval(), error.retry_after)
    cur.execute("""
        INSERT INTO plugin_github_state (plugin_id, checked_at, status, error_code, message, failures, next_attempt_at)
        VALUES (%(id)s, now(), 'error', %(code)s, %(msg)s, %(n)s, now() + make_interval(secs => %(wait)s))
        ON CONFLICT (plugin_id) DO UPDATE SET
            checked_at = now(), status = 'error', error_code = %(code)s, message = %(msg)s,
            failures = %(n)s, next_attempt_at = now() + make_interval(secs => %(wait)s), requested_at = NULL
    """, {"id": plugin_id, "code": error.code, "msg": str(error)[:500], "n": failures, "wait": wait})


def request_check(plugin_id: int) -> dict:
    """« Vérifier maintenant » : dépose la demande ; le worker la relève à son
    prochain réveil (≤ 15 s). Aucun appel réseau dans le processus appelant."""
    conn = get_db_connection()
    if conn is None:
        return {"statut": "pas_de_base"}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO plugin_github_state (plugin_id, requested_at) VALUES (%s, now())
                ON CONFLICT (plugin_id) DO UPDATE SET requested_at = now()
            """, (plugin_id,))
        conn.commit()
        return {"statut": "demande_transmise", "delai_max_s": int(_TIC_SECONDES)}
    except Exception:
        logger.exception("github: dépôt de la demande de vérification en échec")
        _rollback(conn)
        return {"statut": "erreur"}
    finally:
        _close(conn)


def reset(cur, plugin_id: int, *, request: bool) -> None:
    """Oublie le relevé et l'état d'un plugin (source changée ou retirée).

    `request` : dépose aussitôt une demande de vérification pour la nouvelle source.
    """
    cur.execute("DELETE FROM plugin_github_releases WHERE plugin_id = %s", (plugin_id,))
    cur.execute("DELETE FROM plugin_github_state WHERE plugin_id = %s", (plugin_id,))
    if request:
        cur.execute("INSERT INTO plugin_github_state (plugin_id, requested_at) VALUES (%s, now())",
                    (plugin_id,))


def view(cur, plugin: dict) -> dict:
    """Ce que la fiche du plugin montre : source, état du relevé, releases."""
    out: dict = {"source": None, "source_error": None, "state": None, "releases": [],
                 "new_count": 0, "token": token_configured(), "poll_interval": poll_interval(),
                 "channels": [{"id": c, "label": CHANNEL_LABELS[c]} for c in CHANNELS]}
    try:
        out["source"] = normalize_source(plugin.get("github_source"))
    except SourceError as e:
        out["source_error"] = str(e)
    if out["source"] is None and out["source_error"] is None:
        return out
    out["state"] = get_state(cur, plugin["id"])
    for row in list_releases(cur, plugin["id"]):
        row["nature_label"] = nature_label(row["nature"])
        out["releases"].append(row)
    out["new_count"] = sum(1 for r in out["releases"] if r["state"] == STATE_AVAILABLE)
    return out


# ── Cycle du worker ──────────────────────────────────────────────────────────

# Verrou consultatif : un seul processus relève à la fois.
_LOCK_ID = 727270108
ROLES = frozenset({"worker", "all"})
_TIC_SECONDES = 15.0


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _due_plugins(cur) -> list[dict]:
    """Plugins à relever maintenant : demande déposée, ou échéance atteinte."""
    cur.execute("""
        SELECT p.id, p.slug, p.github_source, s.status, s.error_code, s.requested_at,
               (s.next_attempt_at IS NULL OR s.next_attempt_at <= now()) AS elapsed
        FROM plugins p LEFT JOIN plugin_github_state s ON s.plugin_id = p.id
        WHERE p.github_source IS NOT NULL AND p.status <> 'removed'
        ORDER BY p.id
    """)
    periodic = poll_interval() > 0
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    return [r for r in rows if r["requested_at"] is not None or (periodic and r["elapsed"])]


def refresh_plugin(conn, plugin: dict, *, cache: dict | None = None,
                   client: httpx.Client | None = None) -> dict:
    """Relève les releases d'un plugin et note le résultat. Ne lève jamais.

    `plugin` : {id, slug, github_source, status, error_code} (état précédent).
    `cache` : releases déjà lues dans ce cycle, par dépôt (un dépôt qui publie
    plusieurs plugins n'est interrogé qu'une fois).
    """
    cache = cache if cache is not None else {}
    try:
        try:
            source = normalize_source(plugin.get("github_source"))
        except SourceError as e:
            raise GithubError(ERR_SOURCE, str(e)) from None
        if source is None:
            return {"statut": "sans_source"}
        repository = source["repository"]
        if repository not in cache:
            try:
                cache[repository] = fetch_releases(repository, client=client)
            except GithubError as e:
                cache[repository] = e
        if isinstance(cache[repository], GithubError):
            raise cache[repository]
        with conn.cursor() as cur:
            rows = classify(cache[repository], source, known_versions(cur, plugin["id"]))
            store_releases(cur, plugin["id"], rows)
            _record_success(cur, plugin["id"])
        conn.commit()
        if plugin.get("status") == "error":
            logger.info("github: relevé de %s rétabli (%d release(s))", plugin.get("slug"), len(rows))
        return {"statut": "ok", "releases": len(rows)}
    except Exception as exc:
        _rollback(conn)
        error = exc if isinstance(exc, GithubError) else GithubError(
            ERR_INTERNAL, f"Erreur interne pendant le relevé ({type(exc).__name__}).")
        # Une ligne au journal par changement d'état, pas par essai.
        if plugin.get("status") != "error" or plugin.get("error_code") != error.code:
            if error.code == ERR_INTERNAL:
                logger.exception("github: relevé de %s en échec", plugin.get("slug"))
            else:
                logger.warning("github: relevé de %s en échec (%s) : %s — essais espacés, "
                               "prochain message au rétablissement", plugin.get("slug"), error.code, error)
        try:
            with conn.cursor() as cur:
                _record_failure(cur, plugin["id"], error)
            conn.commit()
        except Exception:
            _rollback(conn)
            logger.debug("github: état du relevé non enregistré", exc_info=True)
        return {"statut": "erreur", "code": error.code, "message": str(error)}


def run_cycle(*, client: httpx.Client | None = None) -> dict:
    """Relève tous les plugins dont c'est le tour. Ne lève jamais ; rend un résumé."""
    conn = get_db_connection()
    if conn is None:
        return {"statut": "pas_de_base"}
    locked = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
            locked = bool(cur.fetchone()[0])
            if not locked:
                conn.commit()
                return {"statut": "occupe"}
            due = _due_plugins(cur)
        conn.commit()
        cache: dict = {}
        results = {p["id"]: refresh_plugin(conn, p, cache=cache, client=client) for p in due}
        return {"statut": "fait", "plugins": results}
    finally:
        _rollback(conn)
        if locked:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
                conn.commit()
            except Exception:
                _rollback(conn)
        _close(conn)


def runs_here(mode: str | None = None) -> bool:
    """Ce processus porte-t-il le relevé périodique ? (worker, ou mode « all ».)"""
    if mode is None:
        from ..settings import settings
        mode = str(settings.runtime_mode or "api")
    return mode.strip().lower() in ROLES


def background_loop(stop_event: threading.Event) -> None:
    """Réveil toutes les ~15 s : relève les plugins dus et les demandes déposées."""
    logger.info("github: boucle de relevé démarrée (intervalle %s s)", poll_interval())
    failing = False
    while not stop_event.is_set():
        stop_event.wait(_TIC_SECONDES)
        if stop_event.is_set():
            break
        try:
            run_cycle()
            failing = False
        except Exception:
            # Base indisponible, schéma pas encore migré… : une ligne, pas une par réveil.
            if not failing:
                logger.exception("github: cycle de relevé en échec (signalé une fois)")
            failing = True
    logger.info("github: boucle de relevé arrêtée")


def start_background(stop_event: threading.Event, mode: str | None = None) -> threading.Thread | None:
    """Lance la boucle si ce rôle relève ; sinon None, et rien ne tourne."""
    if not runs_here(mode):
        return None
    thread = threading.Thread(target=background_loop, args=(stop_event,),
                              daemon=True, name="dm-github-releases")
    thread.start()
    return thread
