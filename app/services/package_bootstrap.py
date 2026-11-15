"""Liste de Device Management inscrite dans un paquet de plugin (issue #133).

Un paquet porte en dur la liste des DM que le poste interrogera. Servi tel quel
par un DM que cette liste ne nomme pas, il envoie ses postes ailleurs : le DM
qui l'a servi ne les revoit jamais (ni présence, ni campagne).

Règle : **un paquet servi par un DM ramène ses postes vers ce DM.** À l'envoi,
la liste du paquet est lue, comparée à celle de l'instance (``DM_BOOTSTRAP_URLS``,
à défaut ``PUBLIC_BASE_URL``) et, si elle diffère, réécrite dans l'archive ;
l'empreinte est calculée par l'appelant sur le paquet rendu ici, donc réécrit.

Formats reconnus, par le contenu de l'archive et non par le type déclaré :

- ``thunderbird-prefs`` — ``defaults/preferences/*.js``, ligne
  ``pref('extensions.<X>.bootstrapUrls', 'url1,url2');`` (liste CSV). La
  préférence voisine au singulier (dernier DM gagnant) n'est jamais touchée ;
- ``libreoffice-config`` — ``config.default.json`` à la racine de l'OXT
  (``bootstrap_urls``, ``bootstrap_url``), et les ``<src>`` du bloc
  ``<update-information>`` de ``description.xml`` qui en sont dérivés ;
- ``browser-config`` — ``src/dm/config.json`` d'une extension de navigateur
  (``bootstrap_urls`` / ``bootstrap_url`` du profil actif).

Seule la valeur par défaut du paquet est réécrite. Un paquet signé n'est pas
réécrit (la signature ne survivrait pas) : il est refusé si sa liste diffère,
sauf conservation demandée explicitement (``keep=True``).

Le même passage retire ``dm-config.json`` et ``dm-manifest.json`` de l'archive,
comme avant : une seule réécriture du ZIP.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import zipfile

from .. import runtime_config

STRIP_NAMES = frozenset({"dm-config.json", "dm_config.json", "dm-manifest.json", "dm_manifest.json"})

FORMAT_LABELS = {
    "thunderbird-prefs": "Thunderbird (defaults/preferences)",
    "libreoffice-config": "LibreOffice (config.default.json)",
    "browser-config": "extension de navigateur (src/dm/config.json)",
}

# Statuts d'un relevé (clé « status ») :
ALIGNED = "aligned"                    # la liste du paquet est celle de l'instance
REWRITTEN = "rewritten"                # réécrite à l'envoi
KEPT = "kept"                          # différente, conservée à la demande (paquet signé)
NONE = "none"                          # aucune liste de DM reconnue dans le paquet
OFFLINE = "offline"                    # paquet hors ligne (LibreOffice enabled:false)
INSTANCE_UNKNOWN = "instance_unknown"  # l'instance ne connaît pas sa propre adresse
INSTANCE_INVALID = "instance_invalid"  # adresse d'instance impropre à l'inscription
UNREADABLE = "unreadable"              # archive illisible

_PREFS_FILE_RE = re.compile(r"(^|/)defaults/preferences/[^/]+\.js$", re.IGNORECASE)
_PREFS_LINE_RE = re.compile(
    r"""(pref\(\s*(['"])extensions\.[A-Za-z0-9_.-]+\.bootstrapUrls\2\s*,\s*(['"]))(.*?)(\3\s*\))"""
)
_LO_CONFIG = "config.default.json"
_LO_DESCRIPTION = "description.xml"
_BROWSER_CONFIG = "src/dm/config.json"
_UPDATE_INFO_RE = re.compile(r"(<update-information>)(.*?)(</update-information>)", re.DOTALL)
_SRC_RE = re.compile(r"""<src\s+xlink:href=(["'])(.*?)\1\s*/>""")
_SIGNATURE_NAMES = frozenset({
    "meta-inf/mozilla.rsa", "meta-inf/mozilla.sf", "meta-inf/cose.sig", "meta-inf/cose.manifest",
    "meta-inf/documentsignatures.xml", "meta-inf/macrosignatures.xml",
    "meta-inf/packagesignatures.xml",
})
_SIGNATURE_SUFFIXES = (".rsa", ".dsa", ".ec", ".sf")
# Une adresse d'instance est inscrite dans du JavaScript, du JSON et du XML :
# tout caractère qui sortirait de la chaîne est refusé d'emblée.
_SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/\[\]%@!$&()*+;=-]+$")
_PLACEHOLDER_RE = re.compile(r"^__[A-Z0-9_]+__$|\$\{\{")


class SignedPackageMismatch(ValueError):
    """Paquet signé dont la liste de DM n'est pas celle de l'instance."""

    def __init__(self, message: str, info: dict):
        super().__init__(message)
        self.info = info


# ─── Adresses ───────────────────────────────────────────────────────────────

def normalize_url(url) -> str:
    """Forme comparable : sans espaces ni barre finale, schéma et hôte en minuscules."""
    text = str(url or "").strip().rstrip("/")
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", text)
    if not match:
        return text
    return match.group(1).lower() + match.group(2).lower() + match.group(3)


def clean_urls(urls) -> list[str]:
    """Liste ordonnée, sans vide, sans jeton de gabarit, sans doublon."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        if not isinstance(raw, str):
            continue
        text = raw.strip().rstrip("/")
        if not text or _PLACEHOLDER_RE.search(text):
            continue
        key = normalize_url(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def same_list(a, b) -> bool:
    """Même liste, dans le même ordre (l'ordre est la priorité)."""
    return [normalize_url(u) for u in clean_urls(a)] == [normalize_url(u) for u in clean_urls(b)]


def instance_urls() -> list[str]:
    """Liste de DM que l'instance annonce : DM_BOOTSTRAP_URLS, à défaut PUBLIC_BASE_URL."""
    urls = clean_urls(runtime_config.cfg("DM_BOOTSTRAP_URLS", as_list=True) or [])
    if urls:
        return urls
    return clean_urls([os.environ.get("PUBLIC_BASE_URL") or ""])


def invalid_urls(urls) -> list[str]:
    return [u for u in urls if not _SAFE_URL_RE.match(u)]


# ─── Lecture ────────────────────────────────────────────────────────────────

def is_signed(data: bytes, names=None) -> bool:
    """CRX (en-tête Cr24) ou archive portant une signature dans META-INF/."""
    if data[:4] == b"Cr24":
        return True
    if names is None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile:
            return False
    for name in names:
        low = name.lower()
        if low in _SIGNATURE_NAMES:
            return True
        if low.startswith("meta-inf/") and low.endswith(_SIGNATURE_SUFFIXES):
            return True
    return False


def _json_object(raw: bytes) -> dict | None:
    try:
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_urls(section: dict) -> list[str]:
    urls = section.get("bootstrap_urls")
    urls = list(urls) if isinstance(urls, list) else []
    single = section.get("bootstrap_url")
    if isinstance(single, str) and single.strip():
        urls.append(single)
    return clean_urls(urls)


def _json_set_urls(section: dict, urls: list[str]) -> None:
    section["bootstrap_urls"] = list(urls)
    # La clé au singulier n'est mise à jour que si le paquet la renseigne déjà.
    if isinstance(section.get("bootstrap_url"), str) and section["bootstrap_url"].strip():
        section["bootstrap_url"] = urls[0]


def _json_bytes(obj: dict, original: bytes) -> bytes:
    indent = 2 if b'\n  "' in original else 4
    return (json.dumps(obj, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")


def _find_carriers(zf: zipfile.ZipFile) -> tuple[list[dict], str | None]:
    """Emplacements du paquet qui nomment des DM → ([{format, file, urls}], statut particulier)."""
    names = zf.namelist()
    carriers: list[dict] = []
    special = None

    for name in names:
        if not _PREFS_FILE_RE.search(name):
            continue
        text = zf.read(name).decode("utf-8", errors="replace")
        values = [m.group(4) for m in _PREFS_LINE_RE.finditer(text)]
        if values:
            carriers.append({"format": "thunderbird-prefs", "file": name,
                             "urls": clean_urls(",".join(values).split(","))})

    if _LO_CONFIG in names:
        cfg = _json_object(zf.read(_LO_CONFIG))
        if cfg is not None and ("bootstrap_urls" in cfg or "bootstrap_url" in cfg):
            if cfg.get("enabled") is False:
                special = OFFLINE
            else:
                carriers.append({"format": "libreoffice-config", "file": _LO_CONFIG,
                                 "urls": _json_urls(cfg)})

    if _BROWSER_CONFIG in names:
        cfg = _json_object(zf.read(_BROWSER_CONFIG))
        if cfg is not None:
            profile = str(cfg.get("activeProfile") or "")
            merged = dict(cfg.get("default") or {}) if isinstance(cfg.get("default"), dict) else {}
            if isinstance(cfg.get(profile), dict):
                merged.update(cfg[profile])
                if "bootstrap_urls" in merged or "bootstrap_url" in merged:
                    carriers.append({"format": "browser-config", "file": _BROWSER_CONFIG,
                                     "profile": profile, "urls": _json_urls(merged)})
    return carriers, special


def inspect(source) -> dict:
    """Relevé de ce que porte un paquet (octets ou chemin de fichier), sans le modifier."""
    try:
        if isinstance(source, (bytes, bytearray)):
            head, handle = bytes(source[:4]), io.BytesIO(source)
        else:
            with open(source, "rb") as fh:
                head = fh.read(4)
            handle = source
        with zipfile.ZipFile(handle) as zf:
            carriers, special = _find_carriers(zf)
            signed = is_signed(head, zf.namelist())
    except (zipfile.BadZipFile, OSError, KeyError):
        return {"status": UNREADABLE, "format": None, "file": None, "urls": [], "signed": False}
    first = carriers[0] if carriers else {}
    return {
        "status": special or (None if carriers else NONE),
        "format": first.get("format"),
        "file": first.get("file"),
        "urls": first.get("urls", []),
        "carriers": carriers,
        "signed": signed,
    }


# ─── Réécriture ─────────────────────────────────────────────────────────────

def _rewrite_prefs(raw: bytes, urls: list[str]) -> bytes:
    text = raw.decode("utf-8", errors="surrogateescape")
    csv = ",".join(urls)
    text = _PREFS_LINE_RE.sub(lambda m: m.group(1) + csv + m.group(5), text)
    return text.encode("utf-8", errors="surrogateescape")


def _rewrite_update_feed(raw: bytes, package_urls: list[str], urls: list[str]) -> tuple[bytes, str | None]:
    """Réécrit les <src> du flux de mise à jour natif LibreOffice dérivés de la liste."""
    text = raw.decode("utf-8", errors="surrogateescape")
    block = _UPDATE_INFO_RE.search(text)
    if not block:
        return raw, None
    hrefs = [m.group(2) for m in _SRC_RE.finditer(block.group(2))]
    if not hrefs:
        return raw, None
    bases = sorted({u.rstrip("/") for u in package_urls}, key=len, reverse=True)
    suffixes = set()
    for href in hrefs:
        base = next((b for b in bases if normalize_url(href).startswith(normalize_url(b) + "/")), None)
        if base is None:
            return raw, ("Le flux de mise à jour natif (description.xml) ne dérive pas de la liste "
                         "du paquet : il n'a pas été réécrit.")
        suffixes.add(href[len(base):])
    if len(suffixes) != 1:
        return raw, "Le flux de mise à jour natif (description.xml) n'a pas été réécrit : chemins hétérogènes."
    # Le chemin repris du paquet est déjà du texte d'attribut XML ; l'adresse de
    # l'instance, elle, est échappée (elle peut porter un « & »).
    suffix = suffixes.pop()
    if '"' in suffix:
        return raw, "Le flux de mise à jour natif (description.xml) n'a pas été réécrit : chemin inattendu."
    inner = "\n" + "".join(f'    <src xlink:href="{html.escape(u)}{suffix}"/>\n' for u in urls) + "  "
    new_text = text[:block.start(2)] + inner + text[block.end(2):]
    return new_text.encode("utf-8", errors="surrogateescape"), None


def _replacements(zf: zipfile.ZipFile, carriers: list[dict], urls: list[str]) -> tuple[dict, list[str]]:
    """{nom d'entrée: nouveau contenu} pour inscrire `urls` dans chaque emplacement."""
    out: dict[str, bytes] = {}
    notes: list[str] = []
    for carrier in carriers:
        name = carrier["file"]
        raw = zf.read(name)
        if carrier["format"] == "thunderbird-prefs":
            out[name] = _rewrite_prefs(raw, urls)
        elif carrier["format"] == "libreoffice-config":
            cfg = _json_object(raw) or {}
            _json_set_urls(cfg, urls)
            out[name] = _json_bytes(cfg, raw)
            if _LO_DESCRIPTION in zf.namelist():
                new_desc, note = _rewrite_update_feed(zf.read(_LO_DESCRIPTION), carrier["urls"], urls)
                if note:
                    notes.append(note)
                else:
                    out[_LO_DESCRIPTION] = new_desc
        elif carrier["format"] == "browser-config":
            cfg = _json_object(raw) or {}
            section = cfg.get(carrier.get("profile") or "")
            if isinstance(section, dict):
                _json_set_urls(section, urls)
                out[name] = _json_bytes(cfg, raw)
                notes.append("Les adresses de repli codées dans les scripts de l'extension ne sont pas "
                             "réécrites ; elles ne servent que si src/dm/config.json est illisible.")
    return out, notes


def _rebuild(src: zipfile.ZipFile, replacements: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename.lower() in STRIP_NAMES:
                continue
            if item.filename in replacements:
                dst.writestr(item, replacements[item.filename])
            else:
                dst.writestr(item, src.read(item.filename))
    return buf.getvalue()


def _join(urls) -> str:
    return ", ".join(urls) if urls else "aucune adresse"


def prepare(data: bytes, *, keep: bool = False, instance: list[str] | None = None) -> tuple[bytes, dict]:
    """Paquet tel qu'il sera stocké et servi, et relevé de sa liste de DM.

    Retire dm-config.json / dm-manifest.json, puis inscrit la liste de
    l'instance si celle du paquet diffère. Lève ``zipfile.BadZipFile`` si
    l'archive est illisible, ``SignedPackageMismatch`` pour un paquet signé
    dont la liste diffère (sauf ``keep``).
    """
    instance = clean_urls(instance if instance is not None else instance_urls())
    src = zipfile.ZipFile(io.BytesIO(data))
    carriers, special = _find_carriers(src)
    signed = is_signed(data, src.namelist())
    first = carriers[0] if carriers else {}
    package_urls = first.get("urls", [])
    info = {
        "format": first.get("format"),
        "file": first.get("file"),
        "package_urls": package_urls,
        "served_urls": package_urls,
        "instance_urls": instance,
        "signed": signed,
        "notes": [],
        "message": "",
    }
    replacements: dict[str, bytes] = {}

    if not carriers:
        info["status"] = special or NONE
    elif not instance:
        info["status"] = INSTANCE_UNKNOWN
        info["message"] = (
            f"Ce paquet enverra les postes vers {_join(package_urls)}. L'instance ne connaît pas sa "
            "propre adresse (DM_BOOTSTRAP_URLS et PUBLIC_BASE_URL vides) : la liste n'a été ni "
            "contrôlée ni réécrite.")
    elif invalid_urls(instance):
        info["status"] = INSTANCE_INVALID
        info["message"] = (
            f"Ce paquet enverra les postes vers {_join(package_urls)}. L'adresse d'instance "
            f"{_join(invalid_urls(instance))} ne peut pas être inscrite dans un paquet "
            "(attendu http(s)://hôte[/chemin], sans virgule ni guillemet) : la liste n'a pas été réécrite.")
    elif all(same_list(c["urls"], instance) for c in carriers):
        info["status"] = ALIGNED
    elif signed:
        info["message"] = (
            f"Ce paquet est signé et enverra les postes vers {_join(package_urls)}, pas vers cette "
            f"instance ({_join(instance)}). Le réécrire casserait sa signature : reconstruisez-le avec "
            "la liste de cette instance, ou confirmez qu'il doit être servi tel quel.")
        if not keep:
            info["status"] = "refused"
            raise SignedPackageMismatch(info["message"], info)
        info["status"] = KEPT
        info["message"] = (
            f"Paquet signé servi tel quel : il enverra les postes vers {_join(package_urls)}, pas "
            f"vers cette instance ({_join(instance)}).")
    else:
        replacements, notes = _replacements(src, carriers, instance)
        info["status"] = REWRITTEN
        info["served_urls"] = instance
        info["notes"] = notes
        info["message"] = (
            f"Le paquet envoyé nommait {_join(package_urls)}. Sa liste de DM a été réécrite : le "
            f"paquet servi envoie les postes vers {_join(instance)}.")
    return _rebuild(src, replacements), info


_FILE_MEMO: dict[str, tuple[tuple, dict]] = {}


def inspect_file(path: str | None) -> dict | None:
    """Relevé d'un paquet déjà stocké sur ce disque (envoyé avant le contrôle) ; None si absent."""
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _FILE_MEMO.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    info = inspect(path)
    if len(_FILE_MEMO) > 512:
        _FILE_MEMO.clear()
    _FILE_MEMO[path] = (stamp, info)
    return info


def describe(info: dict | None, current_instance: list[str] | None = None) -> dict | None:
    """Relevé prêt à afficher dans la fiche d'une version, comparé à l'instance d'aujourd'hui."""
    if not info:
        return None
    current = clean_urls(current_instance if current_instance is not None else instance_urls())
    served = info.get("served_urls")
    if served is None:
        served = info.get("urls") or []
    status = info.get("status")
    has_list = bool(info.get("format")) and status not in (NONE, OFFLINE, UNREADABLE)
    return {
        "status": status,
        "format": info.get("format"),
        "format_label": FORMAT_LABELS.get(info.get("format") or "", ""),
        "file": info.get("file"),
        "served_urls": served,
        "package_urls": info.get("package_urls") or served,
        "rewritten": status == REWRITTEN,
        "signed": bool(info.get("signed")),
        "has_list": has_list,
        # Écart avec l'instance telle qu'elle est configurée AUJOURD'HUI : il
        # apparaît aussi quand DM_BOOTSTRAP_URLS change après l'envoi.
        "mismatch": bool(has_list and current and not same_list(served, current)),
        "instance_urls": current,
        "notes": info.get("notes") or [],
        "message": info.get("message") or "",
    }
