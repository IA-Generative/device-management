"""Import d'un paquet de plugin : point de passage unique de tous les envois.

Enchaîne, dans cet ordre (ADR-0006) :

1. relevé du paquet reçu (empreinte, versions embarquées, nature) ;
2. vérification de la signature d'origine (CI du plugin), selon la politique ;
3. adaptation : configuration embarquée retirée, liste de DM de l'instance
   inscrite (issue #133), ancre de confiance déposée ;
4. signature du paquet tel qu'il sera servi.

Tous les chemins d'envoi (admin et API) appellent `import_package` : une
politique `require` s'applique donc aussi au téléversement manuel.

Module neutre : importable par app.main et app.admin sans cycle.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import zlib

from . import package_bootstrap, package_signing
from .versions import libreoffice_numeric_version, version_nature

# Codes de refus (stables : renvoyés par l'API, affichés et testés).
SIGNATURE_REQUIRED = "signature_required"
SIGNATURE_INVALID = "signature_invalid"
REF_NOT_ALLOWED = "ref_not_allowed"
VERSION_MISMATCH = "version_mismatch"
SIGNING_CONFIG = "signing_config"
UNREADABLE = "unreadable_archive"
PLUGIN_REQUIRED = "plugin_required"
FOREIGN_ANCHOR = "foreign_trust_anchor"

# Statuts du contrôle d'origine (info["build"]["status"]).
BUILD_VERIFIED = "verified"
BUILD_UNSIGNED = "unsigned"
BUILD_UNVERIFIED = "unverified"    # enveloppe fournie, mais aucune clé pour la vérifier
BUILD_UNCHECKED = "unchecked"      # politique « off »

# Une enveloppe fournie qui échoue pour l'une de ces raisons signale un fichier
# altéré ou mal apparié : refus dès que le contrôle est actif, même en « warn ».
_TAMPER_CODES = frozenset({
    package_signing.MALFORMED, package_signing.BAD_SIGNATURE, package_signing.WRONG_KIND,
    package_signing.DIGEST_MISMATCH, package_signing.SLUG_MISMATCH, package_signing.VERSION_MISMATCH,
})

_RDF_VERSION_RE = re.compile(r"<em:version>\s*([^<\s]+)\s*</em:version>|em:version=\"([^\"]+)\"")
_OXT_VERSION_RE = re.compile(r"<version\s+value=\"([^\"]+)\"")
_NAME_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?)\.[A-Za-z0-9]+$")


class PackageRefused(ValueError):
    """Import refusé. `code` est l'un des codes ci-dessus ; `info` le relevé partiel."""

    def __init__(self, code: str, message: str, info: dict | None = None):
        super().__init__(message)
        self.code = code
        self.info = info or {}


def embedded_versions(source) -> dict[str, str]:
    """Versions que le paquet déclare lui-même → {fichier: version}.

    `native` désigne ce que lit le logiciel hôte (manifest.json, install.rdf,
    description.xml) : c'est la version que le poste renverra dans
    X-Plugin-Version. `dm-manifest` est la note de version destinée au DM.
    """
    out: dict[str, str] = {}
    try:
        zf = source if isinstance(source, zipfile.ZipFile) else zipfile.ZipFile(io.BytesIO(source))
    except zipfile.BadZipFile:
        return out
    names = set(zf.namelist())

    def text(name: str) -> str:
        # Une entrée illisible (CRC faux, chiffrée, compression inconnue) vaut « rien
        # à lire » : elle ne doit pas faire sortir l'import avant ses contrôles.
        try:
            return zf.read(name).decode("utf-8", errors="replace")
        except _ZIP_READ_ERRORS:
            return ""

    if "manifest.json" in names:
        try:
            manifest = json.loads(text("manifest.json"))
            # version_name : le numéro lisible d'une extension de navigateur, quand
            # `version` doit rester purement numérique.
            value = manifest.get("version_name") or manifest.get("version")
            if isinstance(value, str) and value.strip():
                out["manifest.json"] = value.strip()
        except (ValueError, AttributeError):
            pass
    if "install.rdf" in names:
        m = _RDF_VERSION_RE.search(text("install.rdf"))
        if m:
            out["install.rdf"] = (m.group(1) or m.group(2)).strip()
    if "description.xml" in names:
        m = _OXT_VERSION_RE.search(text("description.xml"))
        if m:
            out["description.xml"] = m.group(1).strip()
    for name in names:
        if name.rsplit("/", 1)[-1].lower() in ("dm-manifest.json", "dm_manifest.json"):
            try:
                changelog = json.loads(text(name)).get("changelog")
                if isinstance(changelog, list) and changelog and isinstance(changelog[0], dict):
                    value = changelog[0].get("version")
                    if isinstance(value, str) and value.strip():
                        out["dm-manifest.json"] = value.strip()
            except (ValueError, AttributeError):
                pass
            break
    return out


_NATIVE_SOURCES = ("manifest.json", "install.rdf", "description.xml")
# Lire une entrée d'archive peut échouer de bien des façons ; aucune ne doit
# passer pour autre chose qu'une archive illisible.
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, KeyError, OSError, EOFError)

_RDF_ID_RE = re.compile(r"<em:id>\s*([^<\s]+)\s*</em:id>|em:id=\"([^\"]+)\"")
_RDF_TARGET_RE = re.compile(r"<em:targetApplication\b")
_OXT_ID_RE = re.compile(r"<identifier\s+value=\"([^\"]+)\"")


def embedded_identity(source) -> str:
    """Identifiant que le logiciel hôte donne à l'extension, ou "" s'il est illisible.

    C'est le seul nom que le paquet porte lui-même et que le plugin installé
    connaît de façon sûre : il lie la signature à « une mise à jour de moi »,
    là où le nom au catalogue du DM peut différer de celui que le poste utilise.
    """
    try:
        zf = source if isinstance(source, zipfile.ZipFile) else zipfile.ZipFile(io.BytesIO(source))
    except zipfile.BadZipFile:
        return ""
    names = set(zf.namelist())

    def text(name: str) -> str:
        try:
            return zf.read(name).decode("utf-8", errors="replace")
        except _ZIP_READ_ERRORS:
            return ""

    if "manifest.json" in names:
        try:
            manifest = json.loads(text("manifest.json"))
            settings = manifest.get("browser_specific_settings") or manifest.get("applications") or {}
            for host in ("gecko", "thunderbird"):
                value = (settings.get(host) or {}).get("id") if isinstance(settings, dict) else None
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except (ValueError, AttributeError):
            pass
    if "install.rdf" in names:
        rdf = text("install.rdf")
        # L'identifiant de l'extension précède ceux des applications cibles.
        target = _RDF_TARGET_RE.search(rdf)
        m = _RDF_ID_RE.search(rdf[:target.start()] if target else rdf)
        if m:
            return (m.group(1) or m.group(2)).strip()
    if "description.xml" in names:
        m = _OXT_ID_RE.search(text("description.xml"))
        if m:
            return m.group(1).strip()
    return ""


def version_notes(version: str, filename: str, embedded: dict[str, str]) -> tuple[str | None, list[str]]:
    """(refus, remarques) sur la concordance des numéros de version (issue #106).

    Refus si le numéro lu par le logiciel hôte diffère de celui enregistré : le
    poste renverrait un autre numéro que la cible, et une campagne épinglée le
    remettrait à jour à chaque interrogation. Le reste n'est que signalé.
    """
    notes: list[str] = []
    if not version:
        return None, notes
    core = version.split("+", 1)[0].split("-", 1)[0]
    for source in _NATIVE_SOURCES:
        found = embedded.get(source)
        if not found or found == version:
            continue
        if source == "description.xml" and found == libreoffice_numeric_version(version):
            # LibreOffice range mal un suffixe : le manifeste d'une pré-version porte
            # son écriture numérique, et le plugin annonce au DM le numéro complet.
            continue
        if found == core and core != version and source != "install.rdf":
            # Certains logiciels hôtes n'acceptent qu'un numéro purement numérique
            # dans leur manifeste (extensions de navigateur, LibreOffice) : une
            # pré-version y porte le cœur X.Y.Z. On l'accepte, en le disant.
            notes.append(
                f"Le manifeste du paquet ({source}) annonce {found}, le cœur de {version}. Si le poste "
                f"renvoie {found} au DM, une campagne d'essai le remettra à jour à chaque interrogation : "
                "le plugin doit annoncer le numéro complet.")
            continue
        return (f"Le paquet se déclare en version {found} ({source}), pas {version}. Le poste "
                f"annoncerait {found} : enregistrez le paquet sous ce numéro, ou reconstruisez-le."), notes
    found = embedded.get("dm-manifest.json")
    if found and found != version:
        notes.append(f"La note de version du paquet (dm-manifest.json) annonce {found}, pas {version}.")
    m = _NAME_VERSION_RE.search(filename or "")
    if m and m.group(1) != version:
        notes.append(f"Le nom du fichier annonce {m.group(1)}, pas {version}.")
    return None, notes


def _check_build(data: bytes, envelope, *, slug: str, version: str, confirm_ref: bool, info: dict,
                 slug_aliases=()) -> dict:
    """Contrôle d'origine selon la politique. Lève PackageRefused."""
    policy = package_signing.policy()
    if policy == package_signing.POLICY_OFF:
        return {"status": BUILD_UNCHECKED}
    if policy == package_signing.POLICY_REQUIRE and not slug:
        # Sans le nom du plugin, rien ne lie la signature à ce qu'on enregistre :
        # un paquet signé pour un autre plugin passerait.
        raise PackageRefused(
            PLUGIN_REQUIRED,
            "Cette instance exige une signature d'origine, et ce chemin d'envoi ne dit pas pour quel "
            "plugin est le paquet. Envoyez-le par « Nouvelle version » de la fiche du plugin, ou par "
            "POST /api/plugins/{slug}/deploy.", info)
    try:
        result = package_signing.verify_build(envelope, data, slug=slug, version=version,
                                              slug_aliases=slug_aliases)
    except package_signing.SigningConfigError as e:
        raise PackageRefused(SIGNING_CONFIG, f"Contrôle des signatures mal configuré : {e}", info) from e
    except package_signing.SignatureError as e:
        if e.code in _TAMPER_CODES:
            raise PackageRefused(SIGNATURE_INVALID, str(e), info) from e
        if policy == package_signing.POLICY_REQUIRE:
            raise PackageRefused(
                SIGNATURE_REQUIRED,
                f"{e} Cette instance n'accepte que des paquets dont la signature d'origine est "
                "vérifiée : joignez le fichier .sig.json publié avec le paquet.", info) from e
        status = BUILD_UNSIGNED if e.code == package_signing.NO_ENVELOPE else BUILD_UNVERIFIED
        return {"status": status, "code": e.code, "message": str(e)}
    source = result["source"]
    build = {
        "status": BUILD_VERIFIED,
        "keyid": result["keyid"],
        "repository": source.get("repository"),
        "ref": source.get("ref"),
        "commit": source.get("commit"),
        "run_id": source.get("run_id"),
        "ref_allowed": package_signing.ref_allowed(source),
    }
    if not build["ref_allowed"]:
        build["message"] = (
            f"Ce paquet a été construit depuis {source.get('ref') or 'une référence inconnue'} "
            f"({source.get('repository') or 'dépôt inconnu'}), hors des références acceptées "
            "par cette instance.")
        if not confirm_ref:
            raise PackageRefused(REF_NOT_ALLOWED,
                                 build["message"] + " Confirmez pour l'importer quand même.",
                                 {**info, "build": build})
        build["ref_confirmed"] = True
    return build


def import_package(data: bytes, *, slug: str = "", version: str = "", filename: str = "",
                   build_signature=None, keep_bootstrap: bool = False,
                   confirm_ref: bool = False, slug_aliases=()) -> tuple[bytes, dict]:
    """Paquet tel qu'il sera stocké et servi, et relevé complet de l'import.

    Le relevé reprend celui de `package_bootstrap.prepare` (liste de DM) et y
    ajoute : `nature`, `upstream` (empreinte du paquet reçu), `build` (contrôle
    d'origine), `versions` (numéros embarqués), `signature` (enveloppe du paquet
    servi, ou None).

    Lève `zipfile.BadZipFile`, `package_bootstrap.SignedPackageMismatch` ou
    `PackageRefused`.
    """
    info: dict = {
        "nature": version_nature(version),
        "upstream": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        "versions": {},
    }
    # Le contrôle d'origine porte sur les octets reçus, avant toute lecture de
    # l'archive : une archive qui se lit mal ne doit pas le faire sauter.
    build = _check_build(data, build_signature, slug=slug, version=version,
                         confirm_ref=confirm_ref, info=info, slug_aliases=slug_aliases)

    try:
        trust = package_signing.trust_document()
    except package_signing.SigningConfigError as e:
        raise PackageRefused(SIGNING_CONFIG, f"Signature des paquets mal configurée : {e}", info) from e
    # Dès qu'un contrôle est actif, une archive illisible ou ambiguë est refusée ;
    # sans aucun réglage, elle est rendue à l'appelant comme avant (BadZipFile).
    strict = (package_signing.policy() != package_signing.POLICY_OFF or trust is not None
              or package_signing.signing_required())

    embedded = info["versions"] = embedded_versions(data)
    refusal, version_remarks = version_notes(version, filename, embedded)
    if refusal:
        raise PackageRefused(VERSION_MISMATCH, refusal, info)

    extra = {}
    if trust is not None:
        extra[package_signing.TRUST_FILE] = (json.dumps(trust, indent=2) + "\n").encode("utf-8")

    try:
        if strict:
            _check_archive(data, info, anchor_expected=trust is not None)
        served, bootstrap_info = package_bootstrap.prepare(data, keep=keep_bootstrap, extra_files=extra)
    except _ZIP_READ_ERRORS as e:
        if strict:
            raise PackageRefused(
                UNREADABLE, "Archive illisible ou endommagée : elle ne peut être ni contrôlée ni signée.",
                info) from e
        raise zipfile.BadZipFile(str(e)) from e
    info.update(bootstrap_info)
    info["build"] = build
    info["notes"] = list(info.get("notes") or []) + version_remarks
    info["trust_anchor"] = package_signing.TRUST_FILE in info.get("changed_files", [])

    instance = bootstrap_info.get("instance_urls") or []
    upstream = {"sha256": info["upstream"]["sha256"],
                "verified": build["status"] == BUILD_VERIFIED,
                "ref": build.get("ref"), "keyid": build.get("keyid")}
    info["signature"] = package_signing.sign_served(
        served, slug=slug, version=version, filename=filename,
        dm=instance[0] if instance else "", upstream=upstream,
        changed_files=info.get("changed_files"), extension_id=embedded_identity(data))
    if info["signature"] is None and package_signing.signing_required():
        raise PackageRefused(
            SIGNING_CONFIG,
            "Ce composant ne détient pas la clé de signature des paquets : l'import doit passer par "
            "le composant d'administration.", info)
    return served, info


def _check_archive(data: bytes, info: dict, *, anchor_expected: bool) -> None:
    """Refuse une archive dont le contenu est ambigu. Lève PackageRefused ou une erreur de lecture.

    - deux entrées de même nom : deux lecteurs n'y voient pas le même fichier ;
    - une archive à signature embarquée qui apporte sa propre ancre de confiance :
      le DM ne peut pas la remplacer sans casser la signature.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n.lower() for n in zf.namelist()]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise PackageRefused(
                UNREADABLE, "Archive refusée : plusieurs entrées portent le même nom "
                f"({', '.join(duplicates[:3])}).", info)
        if (anchor_expected and package_signing.TRUST_FILE in names
                and package_bootstrap.is_signed(data, zf.namelist())):
            raise PackageRefused(
                FOREIGN_ANCHOR, "Cette archive porte une signature embarquée et sa propre ancre de "
                f"confiance ({package_signing.TRUST_FILE}) : le DM ne peut pas y déposer la sienne.", info)
        # Toute entrée doit se lire : sinon le logiciel hôte et le DM ne verraient pas le même paquet.
        bad = zf.testzip()
        if bad:
            raise zipfile.BadZipFile(f"entrée illisible : {bad}")


def preview(data: bytes, *, slug: str = "", version: str = "", filename: str = "",
            build_signature=None, slug_aliases=()) -> dict:
    """Ce que l'import dirait de ce paquet, sans rien stocker ni signer.

    Sert l'aperçu de l'écran d'envoi : nature, numéros embarqués, contrôle
    d'origine. Un refus n'est pas levé : il est rendu dans `refusal`
    ({code, message}), avec `confirmable` quand une confirmation le lèverait.
    """
    embedded = embedded_versions(data)
    out: dict = {"nature": version_nature(version) if version else None, "versions": embedded,
                 "policy": package_signing.policy(), "notes": [], "refusal": None, "build": None}
    try:
        out["build"] = _check_build(data, build_signature, slug=slug, version=version,
                                    confirm_ref=False, info={}, slug_aliases=slug_aliases)
    except PackageRefused as e:
        out["build"] = e.info.get("build")
        out["refusal"] = {"code": e.code, "message": str(e), "confirmable": e.code == REF_NOT_ALLOWED}
    version_refusal, out["notes"] = version_notes(version, filename, embedded)
    if version_refusal and not (out["refusal"] and not out["refusal"]["confirmable"]):
        out["refusal"] = {"code": VERSION_MISMATCH, "message": version_refusal, "confirmable": False}
    return out


def split_info(info: dict | None) -> tuple[dict | None, dict | None, dict | None]:
    """(relevé de la liste de DM, enveloppe du paquet servi, provenance) pour le stockage."""
    if not info:
        return None, None, None
    # `github` : release dont le paquet a été tiré (issue #108), posée par la route d'envoi.
    provenance = {k: info[k] for k in ("nature", "upstream", "build", "versions",
                                       "changed_files", "trust_anchor", "config", "github") if k in info}
    bootstrap = {k: v for k, v in info.items()
                 if k not in ("signature", "upstream", "build", "versions", "nature",
                              "trust_anchor", "config", "github")}
    return bootstrap, info.get("signature"), provenance or None
