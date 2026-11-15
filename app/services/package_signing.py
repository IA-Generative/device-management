"""Signature des paquets de plugin (ADR-0006).

Un seul format pour les deux signataires : la CI du plugin (`kind: "build"`,
paquet publié) et le DM (`kind: "served"`, paquet tel qu'il est servi après
adaptation). Algorithme unique ES256 (ECDSA P-256, SHA-256), signature brute
`r||s` : celle que produit et vérifie WebCrypto, donc le plugin.

Enveloppe (`dm-package-signature/1`) ::

    {"schema": "...", "payload": "<base64>", "signatures": [{"keyid", "alg", "sig"}],
     "delegation": {"payload": "<base64>", "signatures": [...]}}

La signature porte sur les octets du payload décodé : aucune canonisation JSON.

Ce module ne lit que l'environnement (jamais la configuration modifiable à
chaud) : une politique de signature ne doit pas pouvoir être levée depuis
l'interface d'administration.

Module neutre : importable par app.main et app.admin sans cycle.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import fnmatch
import hashlib
import json
import logging
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

logger = logging.getLogger("device-management")

ENVELOPE_SCHEMA = "dm-package-signature/1"
STATEMENT_SCHEMA = "dm-package-statement/1"
DELEGATION_SCHEMA = "dm-signing-delegation/1"
TRUST_SCHEMA = "dm-trust/1"
TRUST_FILE = "dm-trust.json"
ALG = "ES256"

POLICY_OFF = "off"
POLICY_WARN = "warn"
POLICY_REQUIRE = "require"

# Codes d'échec (stables : affichés, tracés et testés).
NO_ENVELOPE = "no_envelope"
MALFORMED = "malformed"
UNKNOWN_KEY = "unknown_key"
BAD_SIGNATURE = "bad_signature"
WRONG_KIND = "wrong_kind"
DIGEST_MISMATCH = "digest_mismatch"
SLUG_MISMATCH = "slug_mismatch"
VERSION_MISMATCH = "version_mismatch"
DELEGATION_INVALID = "delegation_invalid"
NOT_CONFIGURED = "not_configured"


class SignatureError(ValueError):
    """Enveloppe refusée. `code` est l'un des codes ci-dessus."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SigningConfigError(RuntimeError):
    """La configuration de signature du DM est incohérente : on ne signe pas."""


# ─── Clés ───────────────────────────────────────────────────────────────────

def _spki(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER,
                                   serialization.PublicFormat.SubjectPublicKeyInfo)


def key_id(public_key) -> str:
    """16 premiers caractères hexadécimaux du SHA-256 de la clé publique (SPKI DER)."""
    return hashlib.sha256(_spki(public_key)).hexdigest()[:16]


def public_key_b64(public_key) -> str:
    return base64.b64encode(_spki(public_key)).decode("ascii")


def _check_curve(key) -> None:
    curve = getattr(key, "curve", None)
    if not isinstance(curve, ec.SECP256R1):
        raise SigningConfigError("Clé refusée : seule la courbe P-256 (ES256) est acceptée.")


def load_public_keys(pem_text: str | bytes | None) -> dict[str, ec.EllipticCurvePublicKey]:
    """Clés publiques d'un texte PEM (une ou plusieurs) → {keyid: clé}."""
    if not pem_text:
        return {}
    raw = pem_text.encode("utf-8") if isinstance(pem_text, str) else pem_text
    keys: dict[str, ec.EllipticCurvePublicKey] = {}
    marker = b"-----BEGIN PUBLIC KEY-----"
    for chunk in raw.split(marker)[1:]:
        key = serialization.load_pem_public_key(marker + chunk)
        _check_curve(key)
        keys[key_id(key)] = key
    return keys


def public_key_from_b64(text: str) -> ec.EllipticCurvePublicKey:
    key = serialization.load_der_public_key(base64.b64decode(text, validate=True))
    _check_curve(key)
    return key


def load_private_key(pem_text: str | bytes):
    raw = pem_text.encode("utf-8") if isinstance(pem_text, str) else pem_text
    key = serialization.load_pem_private_key(raw, password=None)
    _check_curve(key)
    return key


def _env_text(name: str) -> str:
    """Valeur de `name`, ou contenu du fichier désigné par `name`_FILE (secret monté)."""
    value = os.environ.get(name, "")
    if value.strip():
        return value
    path = os.environ.get(name + "_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as e:
            raise SigningConfigError(f"{name}_FILE illisible : {e}") from e
    return ""


# ─── Enveloppe ──────────────────────────────────────────────────────────────

def _b64d(text, what: str) -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except (binascii.Error, ValueError) as e:
        raise SignatureError(MALFORMED, f"Enveloppe illisible : {what} n'est pas du base64.") from e


def _raw_signature(private_key, payload: bytes) -> bytes:
    der = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _verify_raw(public_key, signature: bytes, payload: bytes) -> bool:
    if len(signature) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(signature[:32], "big"),
                               int.from_bytes(signature[32:], "big"))
    try:
        public_key.verify(der, payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def make_envelope(statement: dict, private_key, delegation: dict | None = None) -> dict:
    """Signe `statement` (sérialisé une fois ; ce sont ces octets qui font foi)."""
    payload = json.dumps(statement, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [{
            "keyid": key_id(private_key.public_key()),
            "alg": ALG,
            "sig": base64.b64encode(_raw_signature(private_key, payload)).decode("ascii"),
        }],
    }
    if delegation:
        envelope["delegation"] = delegation
    return envelope


def parse_envelope(source) -> dict:
    """Enveloppe depuis des octets, du texte ou un dict. Lève SignatureError(MALFORMED)."""
    if isinstance(source, dict):
        envelope = source
    else:
        try:
            text = source.decode("utf-8-sig") if isinstance(source, (bytes, bytearray)) else str(source)
            envelope = json.loads(text)
        except (ValueError, UnicodeDecodeError) as e:
            raise SignatureError(MALFORMED, "Enveloppe illisible : JSON attendu.") from e
    if not isinstance(envelope, dict) or envelope.get("schema") != ENVELOPE_SCHEMA:
        raise SignatureError(MALFORMED, f"Enveloppe illisible : schéma {ENVELOPE_SCHEMA} attendu.")
    if not isinstance(envelope.get("payload"), str) or not isinstance(envelope.get("signatures"), list):
        raise SignatureError(MALFORMED, "Enveloppe illisible : payload ou signatures absents.")
    return envelope


def _signed_payload(part: dict, keys: dict, what: str) -> tuple[bytes, str]:
    """Octets du payload de `part` si une de ses signatures est valide pour `keys`."""
    payload = _b64d(part.get("payload"), f"le payload de {what}")
    signatures = part.get("signatures")
    if not isinstance(signatures, list):
        raise SignatureError(MALFORMED, f"Signatures de {what} illisibles : liste attendue.")
    known = False
    for entry in signatures:
        if not isinstance(entry, dict) or entry.get("alg") != ALG:
            continue
        key = keys.get(str(entry.get("keyid") or ""))
        if key is None:
            continue
        known = True
        try:
            raw = _b64d(entry.get("sig"), f"la signature de {what}")
        except SignatureError:
            continue  # une entrée illisible n'empêche pas d'essayer la suivante
        if _verify_raw(key, raw, payload):
            return payload, str(entry["keyid"])
    if known:
        raise SignatureError(BAD_SIGNATURE, f"Signature de {what} invalide.")
    raise SignatureError(UNKNOWN_KEY, f"Aucune signature de {what} par une clé connue.")


def _json_payload(payload: bytes, schema: str, what: str) -> dict:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SignatureError(MALFORMED, f"{what} illisible.") from e
    if not isinstance(obj, dict) or obj.get("schema") != schema:
        raise SignatureError(MALFORMED, f"{what} illisible : schéma {schema} attendu.")
    return obj


def _parse_date(text) -> datetime.datetime | None:
    try:
        value = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)


def check_delegation(delegation: dict, roots: dict, now: datetime.datetime | None = None) -> dict:
    """Délégation valide (signée par une racine, dans sa fenêtre) → son contenu."""
    if not isinstance(delegation, dict):
        raise SignatureError(DELEGATION_INVALID, "Délégation illisible.")
    try:
        payload, _root = _signed_payload(delegation, roots, "la délégation")
    except SignatureError as e:
        raise SignatureError(DELEGATION_INVALID, str(e)) from e
    content = _json_payload(payload, DELEGATION_SCHEMA, "Délégation")
    now = now or datetime.datetime.now(datetime.UTC)
    not_before, not_after = _parse_date(content.get("not_before")), _parse_date(content.get("not_after"))
    if not_before is None or not_after is None or not (not_before <= now <= not_after):
        raise SignatureError(DELEGATION_INVALID, "Délégation hors de sa période de validité.")
    try:
        key = public_key_from_b64(content.get("public_key") or "")
    except (ValueError, binascii.Error, SigningConfigError) as e:
        raise SignatureError(DELEGATION_INVALID, "Délégation : clé publique illisible.") from e
    if key_id(key) != content.get("keyid"):
        raise SignatureError(DELEGATION_INVALID, "Délégation : keyid incohérent avec la clé.")
    content["_key"] = key
    return content


def verify_envelope(envelope, roots: dict, now: datetime.datetime | None = None,
                    allow_delegation: bool = True) -> tuple[dict, str]:
    """Déclaration signée et keyid du signataire.

    Accepte une signature directe par une clé de `roots`, ou — si
    `allow_delegation` — par une clé couverte par une délégation valide d'une
    de ces clés. La délégation ne dit pas pour quel usage elle vaut : elle
    n'est donc admise que là où elle a un sens (paquet servi par un DM), pas
    pour une signature de build.
    """
    envelope = parse_envelope(envelope)
    keys = dict(roots)
    if allow_delegation and envelope.get("delegation"):
        try:
            content = check_delegation(envelope["delegation"], roots, now)
            keys[content["keyid"]] = content["_key"]
        except SignatureError:
            # Une délégation invalide n'empêche pas une signature directe par une racine.
            if not any(str(s.get("keyid")) in roots for s in envelope["signatures"] if isinstance(s, dict)):
                raise
    payload, signer = _signed_payload(envelope, keys, "l'enveloppe")
    return _json_payload(payload, STATEMENT_SCHEMA, "Déclaration"), signer


# ─── Signature d'origine (CI du plugin) ─────────────────────────────────────

_POLICY_WARNED: set[str] = set()


def policy() -> str:
    """Politique de contrôle de la signature d'origine.

    Vide ou absent : `off`. Une valeur inconnue (« required », « on »…) vaut
    `require`, avec une erreur au journal : une faute de frappe ne doit pas
    désactiver le contrôle sans bruit.
    """
    value = os.environ.get("DM_PACKAGE_SIGNATURE_POLICY", "").strip().lower()
    if not value:
        return POLICY_OFF
    if value in (POLICY_OFF, POLICY_WARN, POLICY_REQUIRE):
        return value
    if value not in _POLICY_WARNED:
        _POLICY_WARNED.add(value)
        logger.error("DM_PACKAGE_SIGNATURE_POLICY=%r inconnu (attendu off, warn ou require) : "
                     "traité comme « require ».", value)
    return POLICY_REQUIRE


def publisher_keys() -> dict:
    """Clés d'éditeur configurées. Lève SigningConfigError si elles sont illisibles."""
    try:
        return load_public_keys(_env_text("DM_PACKAGE_PUBLISHER_KEYS"))
    except ValueError as e:
        raise SigningConfigError("DM_PACKAGE_PUBLISHER_KEYS illisible (clés publiques PEM attendues).") from e


def _patterns(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def ref_allowed(source: dict) -> bool:
    """La référence et le dépôt d'origine sont-ils dans les listes acceptées ?

    Liste vide = pas de restriction (rien n'a été configuré).
    """
    refs = _patterns("DM_PACKAGE_ALLOWED_REFS")
    repos = _patterns("DM_PACKAGE_ALLOWED_REPOSITORIES")
    ref, repo = str(source.get("ref") or ""), str(source.get("repository") or "")
    if refs and not any(fnmatch.fnmatchcase(ref, p) for p in refs):
        return False
    return not repos or any(fnmatch.fnmatchcase(repo, p) for p in repos)


def verify_build(envelope, data: bytes, *, slug: str = "", version: str = "", slug_aliases=()) -> dict:
    """Vérifie l'enveloppe d'origine d'un paquet reçu. Lève SignatureError.

    Renvoie {keyid, source, statement}. `slug` et `version` vides ne sont pas
    comparés (chemins d'envoi qui ne les connaissent pas encore). Le nom signé
    par la CI est accepté s'il est le nom du plugin au catalogue ou l'un de ses
    alias (`slug_aliases`) : le dépôt du plugin et le catalogue ne nomment pas
    toujours le plugin de la même façon.
    """
    if envelope in (None, b"", ""):
        raise SignatureError(NO_ENVELOPE, "Aucune signature d'origine n'accompagne ce paquet.")
    keys = publisher_keys()
    if not keys:
        raise SignatureError(NOT_CONFIGURED,
                             "Aucune clé d'éditeur configurée (DM_PACKAGE_PUBLISHER_KEYS) : "
                             "la signature d'origine ne peut pas être vérifiée.")
    statement, signer = verify_envelope(envelope, keys, allow_delegation=False)
    if statement.get("kind") != "build":
        raise SignatureError(WRONG_KIND, "Cette enveloppe n'est pas une signature de build.")
    # Une déclaration qui ne nomme ni le plugin ni la version vaudrait pour tous.
    for field in ("slug", "version", "sha256"):
        if not isinstance(statement.get(field), str) or not statement[field].strip():
            raise SignatureError(MALFORMED, f"Signature de build incomplète : champ « {field} » absent.")
    digest = hashlib.sha256(data).hexdigest()
    if str(statement.get("sha256") or "").lower() != digest:
        raise SignatureError(DIGEST_MISMATCH,
                             "La signature d'origine ne correspond pas au fichier reçu "
                             f"(signé : {str(statement.get('sha256'))[:12]}…, reçu : {digest[:12]}…).")
    accepted = {slug, *[a for a in slug_aliases if a]}
    if slug and statement.get("slug") and statement["slug"] not in accepted:
        raise SignatureError(SLUG_MISMATCH,
                             f"Ce paquet est signé pour « {statement['slug']} », pas pour « {slug} ».")
    if version and statement.get("version") and statement["version"] != version:
        raise SignatureError(VERSION_MISMATCH,
                             f"Ce paquet est signé en version {statement['version']}, pas {version}.")
    source = statement.get("source") if isinstance(statement.get("source"), dict) else {}
    return {"keyid": signer, "source": source, "statement": statement}


# ─── Signature du DM (paquet servi) ─────────────────────────────────────────

def _signing_material() -> tuple | None:
    """(clé privée, délégation | None, {keyid: racine}) ou None si le DM ne signe pas."""
    pem = _env_text("DM_PACKAGE_SIGNING_KEY")
    if not pem.strip():
        return None
    try:
        key = load_private_key(pem)
    except (ValueError, TypeError) as e:
        raise SigningConfigError("DM_PACKAGE_SIGNING_KEY illisible (clé EC P-256 en PEM attendue).") from e
    own = {key_id(key.public_key()): key.public_key()}
    try:
        roots = load_public_keys(_env_text("DM_PACKAGE_TRUST_ROOTS"))
    except ValueError as e:
        raise SigningConfigError("DM_PACKAGE_TRUST_ROOTS illisible (clés publiques PEM attendues).") from e
    raw_delegation = _env_text("DM_PACKAGE_SIGNING_DELEGATION").strip()
    if not raw_delegation:
        if roots and not set(own) & set(roots):
            raise SigningConfigError(
                "DM_PACKAGE_TRUST_ROOTS est renseigné mais ne contient pas la clé du DM, et aucune "
                "délégation n'est fournie (DM_PACKAGE_SIGNING_DELEGATION).")
        return key, None, roots or own
    if not roots:
        raise SigningConfigError("Une délégation est fournie sans racine (DM_PACKAGE_TRUST_ROOTS).")
    try:
        delegation = json.loads(raw_delegation)
        content = check_delegation(delegation, roots)
    except (ValueError, SignatureError) as e:
        raise SigningConfigError(f"DM_PACKAGE_SIGNING_DELEGATION refusée : {e}") from e
    if content["keyid"] not in own:
        raise SigningConfigError("La délégation ne couvre pas la clé de signature de ce DM.")
    return key, delegation, roots


def signing_required() -> bool:
    """L'instance s'engage-t-elle à ne servir que des paquets qu'elle a signés ?

    `DM_PACKAGE_SIGNING_REQUIRED` se pose sur TOUS les composants, clé ou non :
    c'est ce qui permet à un composant sans clé de refuser un import au lieu de
    stocker un paquet non signé sans rien dire.
    """
    return os.environ.get("DM_PACKAGE_SIGNING_REQUIRED", "").strip().lower() in ("1", "true", "yes", "on")


def signing_enabled() -> bool:
    """Le DM signe-t-il ce qu'il sert ? Lève SigningConfigError si la configuration est incohérente."""
    return _signing_material() is not None


def trust_document() -> dict | None:
    """Contenu de dm-trust.json à déposer dans le paquet, ou None si le DM ne signe pas."""
    material = _signing_material()
    if material is None:
        return None
    _key, _delegation, roots = material
    return {
        "schema": TRUST_SCHEMA,
        "roots": [{"keyid": kid, "alg": ALG, "public_key": public_key_b64(pub)}
                  for kid, pub in sorted(roots.items())],
    }


def sign_served(data: bytes, *, slug: str, version: str, filename: str, dm: str = "",
                upstream: dict | None = None, changed_files: list[str] | None = None,
                extension_id: str = "") -> dict | None:
    """Enveloppe `kind: "served"` du paquet tel qu'il sera servi, ou None si le DM ne signe pas."""
    material = _signing_material()
    if material is None:
        return None
    key, delegation, _roots = material
    statement = {
        "schema": STATEMENT_SCHEMA,
        "kind": "served",
        "slug": slug,
        "version": version,
        "file": filename,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "dm": dm,
        "signed_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upstream": upstream or {},
        "changed_files": sorted(changed_files or []),
    }
    # Identifiant de l'extension dans le logiciel hôte : le plugin installé le
    # compare au sien (« ce paquet est bien une mise à jour de moi »).
    if extension_id:
        statement["extension_id"] = extension_id
    return make_envelope(statement, key, delegation)


def current_delegation() -> dict | None:
    """Délégation configurée sur ce composant, si elle est lisible (elle n'est pas secrète)."""
    raw = _env_text("DM_PACKAGE_SIGNING_DELEGATION").strip()
    if not raw:
        return None
    try:
        delegation = json.loads(raw)
    except ValueError:
        return None
    return delegation if isinstance(delegation, dict) else None


def with_current_delegation(envelope: dict | None) -> dict | None:
    """Enveloppe à servir : la délégation en vigueur remplace celle du jour de l'import.

    Une délégation a une échéance ; l'enveloppe, elle, est stockée une fois.
    Servie avec la délégation de l'import, elle serait refusée par les postes
    dès l'échéance, pour tous les paquets déjà importés. La délégation
    courante n'est jointe que si elle couvre la clé qui a signé l'enveloppe ;
    sinon l'enveloppe est servie telle qu'elle a été stockée.
    """
    if not isinstance(envelope, dict):
        return envelope
    delegation = current_delegation()
    if not delegation:
        return envelope
    try:
        content = json.loads(_b64d(delegation.get("payload"), "le payload de la délégation").decode("utf-8"))
    except (SignatureError, ValueError, UnicodeDecodeError, AttributeError):
        return envelope
    signers = {str(s.get("keyid")) for s in envelope.get("signatures") or [] if isinstance(s, dict)}
    if not isinstance(content, dict) or content.get("keyid") not in signers:
        return envelope
    return {**envelope, "delegation": delegation}


def make_delegation(root_private_key, dm_public_key, *, dm: str,
                    not_before: datetime.datetime, not_after: datetime.datetime) -> dict:
    """Délégation de signature pour la clé d'un DM (outil d'exploitation et tests)."""
    content = {
        "schema": DELEGATION_SCHEMA,
        "dm": dm,
        "keyid": key_id(dm_public_key),
        "public_key": public_key_b64(dm_public_key),
        "not_before": not_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "not_after": not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [{
            "keyid": key_id(root_private_key.public_key()),
            "alg": ALG,
            "sig": base64.b64encode(_raw_signature(root_private_key, payload)).decode("ascii"),
        }],
    }
