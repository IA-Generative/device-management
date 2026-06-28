"""Utilitaires durcis pour l'endpoint LLM de suggestion de catalogue.

Extrait la logique sensible/bloquante de ``POST /admin/api/catalog/suggest`` afin
de la rendre (a) testable isolément, (b) non bloquante pour l'event-loop quand
elle est appelée via ``run_in_threadpool``, (c) résistante aux abus relevés en
audit :

  - SSRF / lecture de fichier local via ``readme_url``  -> ``validate_public_url``
  - zip-bomb / OOM via ``plugin_file``                  -> ``parse_plugin_zip``
  - parsing fragile de la sortie LLM (502/IndexError)   -> ``extract_suggestion_json``
  - injection indirecte (champs non maîtrisés)          -> ``sanitize_suggestion``
  - matraquage de l'endpoint                            -> ``rate_limit_ok``
"""

from __future__ import annotations

import io
import ipaddress
import json
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlparse

from fastapi import HTTPException

# --- Bornes anti-DoS (zip-bomb / gros uploads) -------------------------------
MAX_UPLOAD_BYTES = 25 * 1024 * 1024         # taille brute max de l'upload
MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024   # cumul décompressé max
MAX_FILE_BYTES = 5 * 1024 * 1024            # par fichier extrait
MAX_README_BYTES = 1 * 1024 * 1024          # README distant

INTERESTING_FILES = {
    "manifest.json", "description.xml", "package.json",
    "readme.md", "readme.txt", "readme", "readme.rst",
    "notice-utilisateur.md", "notice-utilisateur.txt",
    "notice_utilisateur.md", "notice_utilisateur.txt",
    "changelog.md", "changelog.txt", "changes.md", "history.md",
    "dm-config.json", "dm_config.json",
    "dm-manifest.json", "dm_manifest.json",
}
ICON_BASENAMES = {"logo.png", "icon128.png", "icon48.png"}

# Clés autorisées dans une suggestion (anti-injection : le reste est jeté).
ALLOWED_SUGGESTION_KEYS = {
    "slug", "name", "description", "intent", "device_type", "category",
    "publisher", "visibility", "homepage_url", "support_email", "icon_url",
    "doc_url", "license", "key_features", "changelog", "oxt_version",
    "oxt_identifier", "config_template",
    "_has_readme", "_has_manifest", "_has_config_template", "_source",
    "icon_data_url",
}
_URL_FIELDS = ("homepage_url", "icon_url", "doc_url", "icon_data_url")
_SAFE_URL_SCHEMES = ("http://", "https://")


# --- Anti-SSRF ---------------------------------------------------------------
def validate_public_url(url: str) -> str:
    """Valide une URL fournie par l'utilisateur avant un fetch serveur (anti-SSRF).

    Autorise uniquement http(s) résolvant vers une IP **publique**. Bloque
    ``file://`` et schémas exotiques, localhost, IP privées/loopback/link-local
    (dont l'endpoint de métadonnées cloud 169.254.169.254). Lève
    ``HTTPException(400)`` sinon, et retourne l'URL d'origine si OK.
    """
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "URL invalide : seules les URL http(s) sont autorisées")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "URL invalide : hôte manquant")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise HTTPException(400, "URL invalide : hôte non résolvable") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise HTTPException(400, "URL non autorisée (cible interne/privée)")
    return raw


# --- Parsing zip borné (anti zip-bomb) ---------------------------------------
def _read_zip_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_FILE_BYTES:
        raise HTTPException(413, "Fichier du plugin trop volumineux")
    with zf.open(name) as fh:
        return fh.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES]


def parse_plugin_zip(pdata: bytes) -> dict:
    """Parse un .oxt/.zip de plugin et en extrait les infos utiles.

    SYNCHRONE et CPU-bound : à appeler via ``run_in_threadpool`` depuis le handler
    async pour ne pas geler l'event-loop. Borne la taille décompressée cumulée
    (anti zip-bomb). Un zip invalide retourne un dict vide (pas d'exception).
    """
    out: dict = {
        "extracted": [], "dm_manifest": None, "has_readme": False,
        "has_manifest": False, "has_config_template": False,
        "config_template": None, "oxt_version": "", "oxt_identifier": "",
        "icon_data": None, "icon_filename": None,
    }
    if len(pdata) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Fichier trop volumineux")
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(pdata)) as zf:
            if sum(i.file_size for i in zf.infolist()) > MAX_DECOMPRESSED_BYTES:
                raise HTTPException(413, "Archive trop volumineuse une fois décompressée")
            for name in zf.namelist():
                basename = name.rsplit("/", 1)[-1].lower()
                if basename in ICON_BASENAMES and "assets/" in name.lower():
                    out["icon_data"] = _read_zip_member(zf, name)
                    out["icon_filename"] = basename
                    continue
                if basename in INTERESTING_FILES:
                    raw = _read_zip_member(zf, name).decode("utf-8", errors="replace")
                    total += len(raw)
                    if total > MAX_DECOMPRESSED_BYTES:
                        break
                    if basename in ("dm-config.json", "dm_config.json"):
                        try:
                            out["config_template"] = json.loads(raw)
                            out["has_config_template"] = True
                        except json.JSONDecodeError:
                            pass
                    elif basename in ("dm-manifest.json", "dm_manifest.json"):
                        try:
                            out["dm_manifest"] = json.loads(raw)
                            out["has_manifest"] = True
                        except json.JSONDecodeError:
                            pass
                    elif basename == "description.xml":
                        try:
                            root = ET.fromstring(raw)
                            ns = {"d": "http://openoffice.org/extensions/description/2006"}
                            ver_el = root.find(".//d:version", ns)
                            if ver_el is not None:
                                out["oxt_version"] = ver_el.get("value", "")
                            ident_el = root.find(".//d:identifier", ns)
                            if ident_el is not None:
                                out["oxt_identifier"] = ident_el.get("value", "")
                        except Exception:
                            pass
                        out["extracted"].append(f"--- {name} ---\n{raw[:8000]}")
                    else:
                        out["extracted"].append(f"--- {name} ---\n{raw[:8000]}")
                        if basename.startswith(("readme", "notice")):
                            out["has_readme"] = True
    except zipfile.BadZipFile:
        pass
    return out


# --- Parsing défensif de la sortie LLM ---------------------------------------
def extract_suggestion_json(content: str) -> dict:
    """Extrait un objet JSON de la réponse LLM, sans IndexError.

    Gère les blocs markdown ```json ... ``` et le JSON nu. Lève ``ValueError``
    (jamais ``IndexError``) si rien d'exploitable — l'appelant renvoie alors une
    502 GÉNÉRIQUE, sans divulguer le détail interne du parsing.
    """
    if not content or not content.strip():
        raise ValueError("réponse LLM vide")
    text = content.strip()
    if "```" in text:
        parts = text.split("```")
        candidate = parts[1] if len(parts) >= 3 else parts[-1]
        if candidate.lstrip().lower().startswith("json"):
            candidate = candidate.lstrip()[4:]
        text = candidate.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("aucun JSON exploitable dans la réponse LLM") from None
        obj = json.loads(m.group(0))  # JSONDecodeError est un ValueError
    if not isinstance(obj, dict):
        raise ValueError("réponse LLM non conforme (objet attendu)")
    return obj


# --- Anti-injection indirecte ------------------------------------------------
def _safe_url_value(val) -> str:
    if not isinstance(val, str):
        return ""
    v = val.strip()
    if v.lower().startswith("data:image/"):   # icônes inline autorisées
        return v
    if v.lower().startswith(_SAFE_URL_SCHEMES):
        return v
    return ""  # rejette javascript:, data:text/html, file:, etc.


def sanitize_suggestion(suggestion: dict) -> dict:
    """Ne garde que les clés connues et neutralise les URLs dangereuses.

    Défense contre l'injection indirecte : la sortie LLM (influencée par un
    README hostile) ne doit pas injecter de ``javascript:``/``data:text`` dans
    les champs URL pré-remplis du catalogue.
    """
    if not isinstance(suggestion, dict):
        return {}
    clean: dict = {}
    for k, v in suggestion.items():
        if k not in ALLOWED_SUGGESTION_KEYS:
            continue
        if k in _URL_FIELDS:
            v = _safe_url_value(v)
        clean[k] = v
    return clean


# --- Rate limit léger (fenêtre glissante par clé, en mémoire) ----------------
_RL_LOCK = threading.Lock()
_RL_HITS: dict[str, list] = {}


def rate_limit_ok(key: str, limit: int, window_seconds: int, now: float | None = None) -> bool:
    """True si sous le seuil, False (= 429) si dépassé. ``limit<=0`` désactive."""
    if limit <= 0:
        return True
    ts = time.time() if now is None else now
    cutoff = ts - window_seconds
    with _RL_LOCK:
        hits = [t for t in _RL_HITS.get(key, []) if t > cutoff]
        if len(hits) >= limit:
            _RL_HITS[key] = hits
            return False
        hits.append(ts)
        _RL_HITS[key] = hits
        return True
