#!/usr/bin/env python3
"""Banc : clés, fichiers d'environnement, paquets neutres et enveloppes de build."""
import datetime
import hashlib
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from cryptography.hazmat.primitives import serialization as S
from cryptography.hazmat.primitives.asymmetric import ec

from app.services import package_signing as ps

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
INSTANCE = "http://localhost:18090"

def new(): return ec.generate_private_key(ec.SECP256R1())
def pub(k): return k.public_key().public_bytes(S.Encoding.PEM, S.PublicFormat.SubjectPublicKeyInfo).decode()
def priv(k): return k.private_bytes(S.Encoding.PEM, S.PrivateFormat.PKCS8, S.NoEncryption()).decode()
def envline(k, v): return f"{k}={json.dumps(v)}\n"   # valeur multi-ligne entre guillemets (compose)

publisher, intruder, root, dm = new(), new(), new(), new()
now = datetime.datetime.now(datetime.UTC)
deleg = ps.make_delegation(root, dm.public_key(), dm=INSTANCE,
                           not_before=now - datetime.timedelta(hours=1),
                           not_after=now + datetime.timedelta(days=365))
mode = sys.argv[1] if len(sys.argv) > 1 else "delegation"

common = {
    "DATABASE_URL": "postgresql://postgres:postgres@pg:5432/bootstrap",
    "DM_DB_AUTO_BOOTSTRAP": "true", "DM_APP_ENV": "dev", "DM_CONFIG_ENABLED": "true",
    "PUBLIC_BASE_URL": INSTANCE, "DM_BINARIES_MODE": "local", "DM_QUEUE_ADMIN_TOKEN": "banc-token",
    "DM_RELAY_SECRET_PEPPER": "banc-pepper", "DM_LLM_TOKEN_SIGNING_KEY": "banc-llm-key",
    "ADMIN_SESSION_SECRET": "banc-session-secret", "DM_ALLOW_ORIGINS": "*",
    "KEYCLOAK_CLIENT_ID": "client-du-banc",
    "DM_GITHUB_API_URL": "http://faux-github:8765", "DM_GITHUB_POLL_INTERVAL": "5",
    "DM_PACKAGE_SIGNATURE_POLICY": "require",
    # banc-lo : plugin dont la chaîne de build « ne signe pas encore ».
    "DM_PACKAGE_SIGNATURE_OPTIONAL_PLUGINS": "banc-lo",
    "DM_PACKAGE_SIGNING_REQUIRED": "true",
    "DM_PACKAGE_PUBLISHER_KEYS": pub(publisher),
    "DM_PACKAGE_ALLOWED_REFS": "refs/heads/main,refs/heads/develop,refs/tags/*",
    "DM_PACKAGE_ALLOWED_REPOSITORIES": "IA-Generative/*",
}
admin = {"DM_PACKAGE_SIGNING_KEY": priv(dm)}
if mode == "delegation":
    # Ni la délégation ni la racine ne sont secrètes : tous les composants les lisent
    # (la délégation en vigueur est jointe à l'enveloppe au moment de servir).
    common["DM_PACKAGE_SIGNING_DELEGATION"] = json.dumps(deleg)
    common["DM_PACKAGE_TRUST_ROOTS"] = pub(root)
open(os.path.join(HERE, "common.env"), "w").writelines(envline(k, v) for k, v in common.items())
open(os.path.join(HERE, "admin.env"), "w").writelines(envline(k, v) for k, v in admin.items())
json.dump({"root_keyid": ps.key_id(root.public_key()), "dm_keyid": ps.key_id(dm.public_key()),
           "publisher_keyid": ps.key_id(publisher.public_key()), "mode": mode, "instance": INSTANCE},
          open(os.path.join(OUT, "keys.json"), "w"), indent=1)

DM_CONFIG = {"configVersion": 1, "default": {"feature": True},
             "prod": {"llm_base_urls": "${{LLM_BASE_URL}}", "keycloakClientId": "client-du-build-en-dur"},
             "local": {"llmToken": "exemple"}}

def xpi(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("install.rdf", f'<?xml version="1.0"?><RDF><Description><em:id>banc@example.invalid</em:id>'
                                  f'<em:version>{version}</em:version></Description></RDF>')
        z.writestr("defaults/preferences/prefs.js",
                   "pref('extensions.Banc.bootstrapUrls', 'https://dm.example.invalid');\n"
                   "pref('extensions.Banc.autre', 'inchangé');\n")
        z.writestr("chrome/content/main.js", f"// code du plugin {version}\n" + "x" * 2000)
        z.writestr("dm-config.json", json.dumps(DM_CONFIG))
        z.writestr("dm-manifest.json", json.dumps({"device_type": "matisse", "changelog": [{"version": version, "changes": ["banc"]}]}))
    return buf.getvalue()

def oxt(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("description.xml", f'<?xml version="1.0"?><description xmlns:xlink="http://www.w3.org/1999/xlink">'
                   f'<identifier value="banc.lo"/><version value="{version}"/>'
                   f'<update-information>\n    <src xlink:href="https://dm.example.invalid/catalog/banc-lo/update.xml"/>\n  </update-information></description>')
        z.writestr("config.default.json", json.dumps({"enabled": True, "bootstrap_urls": ["https://dm.example.invalid"]}, indent=2))
        z.writestr("META-INF/manifest.xml", "<manifest/>")
        z.writestr("dm-config.json", json.dumps(DM_CONFIG))
    return buf.getvalue()

def envelope(data, slug, version, fname, ref, key=publisher, repo="IA-Generative/iassistant-matisse"):
    st = {"schema": ps.STATEMENT_SCHEMA, "kind": "build", "slug": slug, "version": version, "file": fname,
          "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
          "source": {"repository": repo, "ref": ref, "commit": "0" * 40, "workflow": ".github/workflows/x.yml", "run_id": "1"},
          "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return ps.make_envelope(st, key)

CASES = [  # (slug, version, ref, fabrique, ext)
    ("banc-tb", "0.23.0", "refs/heads/main", xpi, "xpi"),
    ("banc-tb", "0.24.0", "refs/heads/main", xpi, "xpi"),
    ("banc-tb", "0.25.0-rc.241", "refs/heads/develop", xpi, "xpi"),
    ("banc-tb", "0.25.0-dev.229", "refs/heads/exp/recherche-v2", xpi, "xpi"),
    ("banc-tb", "0.25.0-dev.236", "refs/heads/exp/chat-agent", xpi, "xpi"),
    ("banc-tb", "0.25.0", "refs/heads/main", xpi, "xpi"),
    ("banc-lo", "0.3.1", "refs/heads/master", oxt, "oxt"),
    ("banc-tb", "0.26.0-dev.300", "refs/heads/exp/ecrans", xpi, "xpi"),
    ("banc-tb", "0.26.0-rc.301", "refs/heads/develop", xpi, "xpi"),
]
for slug, version, ref, make, ext in CASES:
    data = make(version)
    name = f"{slug}-{version}.{ext}"
    open(os.path.join(OUT, name), "wb").write(data)
    json.dump(envelope(data, slug, version, name, ref), open(os.path.join(OUT, name + ".sig.json"), "w"))
# Le dépôt du plugin nomme le plugin autrement que le catalogue : accepté par alias.
data = xpi("0.24.2")
name = "banc-tb-0.24.2.xpi"
open(os.path.join(OUT, name), "wb").write(data)
json.dump(envelope(data, "nom-du-depot", "0.24.2", name, "refs/heads/main"), open(os.path.join(OUT, name + ".sig.json"), "w"))
# Releases « GitHub » servies par le faux serveur : une générale et une candidate.
for version, ref, prerelease in (("0.27.0", "refs/heads/main", False), ("0.27.1-rc.5", "refs/heads/develop", True)):
    folder = os.path.join(OUT, "github", "IA-Generative", "banc-plugin", f"banc-v{version}")
    os.makedirs(folder, exist_ok=True)
    data = xpi(version)
    name = f"banc-tb-{version}.xpi"
    open(os.path.join(folder, name), "wb").write(data)
    json.dump(envelope(data, "banc-tb", version, name, ref), open(os.path.join(folder, name + ".sig.json"), "w"))
    json.dump({"prerelease": prerelease, "body": f"Notes de la {version}"}, open(os.path.join(folder, "release.json"), "w"))

# Paquets LibreOffice SANS signature (chaîne de build qui ne signe pas encore).
for version in ("0.3.2", "0.3.3"):
    open(os.path.join(OUT, f"banc-lo-{version}.oxt"), "wb").write(oxt(version))

# Cas d'attaque
data = xpi("0.24.1")
name = "banc-tb-0.24.1.xpi"
open(os.path.join(OUT, name), "wb").write(data)
json.dump(envelope(data, "banc-tb", "0.24.1", name, "refs/heads/main", key=intruder), open(os.path.join(OUT, name + ".intrus.sig.json"), "w"))
json.dump(envelope(data, "banc-tb", "0.24.1", name, "refs/heads/main"), open(os.path.join(OUT, name + ".sig.json"), "w"))
open(os.path.join(OUT, "banc-tb-0.24.1.altere.xpi"), "wb").write(xpi("0.24.1").replace(b"x" * 10, b"y" * 10) if False else data[:-40] + data[-40:].replace(b"\x00", b"\x00") )
# paquet altéré : même numéro, contenu différent
alt = io.BytesIO()
with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(alt, "w", zipfile.ZIP_DEFLATED) as dst:
    for it in src.infolist():
        dst.writestr(it, src.read(it.filename) + (b"\n// porte derobee" if it.filename.endswith("main.js") else b""))
open(os.path.join(OUT, "banc-tb-0.24.1.altere.xpi"), "wb").write(alt.getvalue())
print("ok", mode, sorted(os.listdir(OUT)))
