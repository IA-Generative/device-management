#!/usr/bin/env python3
"""Banc : scénario de bout en bout de la chaîne de confiance des paquets."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
ADMIN, API = "http://localhost:18091", "http://localhost:18090"
TOKEN = {"X-Admin-Token": "banc-token"}
KEYS = json.load(open(os.path.join(OUT, "keys.json")))
RES = []
def check(name, cond, detail=""):
    RES.append((name, bool(cond), detail))
    print(("  OK  " if cond else "  KO  ") + name + (f"  — {detail}" if detail and not cond else ""))
    return bool(cond)
def section(t): print(f"\n== {t}")
def f(name): return open(os.path.join(OUT, name), "rb").read()
def psql(sql):
    r = subprocess.run(["docker", "compose", "exec", "-T", "pg", "psql", "-U", "postgres", "-d", "bootstrap", "-t", "-A", "-c", sql],
                       capture_output=True, text=True, cwd=HERE)
    if r.returncode:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()
def deploy(base, slug, pkg, sig=None, **fields):
    files = {"binary": (pkg if isinstance(pkg, str) else pkg[0], f(pkg) if isinstance(pkg, str) else pkg[1])}
    if sig:
        files["signature"] = (sig, f(sig))
    return httpx.post(f"{base}/api/plugins/{slug}/deploy", headers=TOKEN, files=files,
                      data={"strategy": "immediate", **fields}, timeout=60)
def config(slug, version, uuid, email=""):
    h = {"X-Plugin-Version": version, "X-Client-UUID": uuid}
    if email:
        h["X-User-Email"] = email
    r = httpx.get(f"{API}/config/{slug}/config.json", headers=h, timeout=30)
    r.raise_for_status()
    return r.json()
def update_of(cfg): return cfg.get("update") or (cfg.get("config") or {}).get("update")
def node_verify(pkg, env, trust, version, slug, now=None):
    with tempfile.TemporaryDirectory() as d:
        open(f"{d}/p", "wb").write(pkg)
        json.dump(env, open(f"{d}/e", "w"))
        json.dump(trust, open(f"{d}/t", "w"))
        r = subprocess.run(["node", os.path.join(HERE, "verify.mjs"), f"{d}/p", f"{d}/e", f"{d}/t", version, slug] + ([now] if now else []),
                           capture_output=True, text=True)
        return (r.stdout.strip() or r.stderr.strip()[-200:])
PLUGIN_VERIFIER = os.environ.get("PLUGIN_VERIFIER", os.path.join(HERE, "plugin-package-signature.js"))
def plugin_verify(pkg, env, trust, version, extension_id="banc@example.invalid", slug="nom-que-le-poste-utilise", now=None):
    """Le module de vérification du plugin Thunderbird lui-même (iassistant-matisse,
    thunderbird/60.9.1/modules/package-signature.js), s'il est fourni : PLUGIN_VERIFIER=<chemin>."""
    with tempfile.TemporaryDirectory() as d:
        open(f"{d}/p", "wb").write(pkg)
        json.dump(env, open(f"{d}/e", "w"))
        json.dump(trust, open(f"{d}/t", "w"))
        r = subprocess.run(["node", os.path.join(HERE, "verify-plugin.cjs"), f"{d}/p", f"{d}/e", f"{d}/t", version, extension_id, slug] + ([now] if now else []),
                           capture_output=True, text=True, env={**os.environ, "PLUGIN_VERIFIER": PLUGIN_VERIFIER})
        return (r.stdout.strip() or r.stderr.strip()[-300:])
def client_update(slug, installed, uuid, email="", trust=None):
    """Ce que ferait le plugin : directive, téléchargement, empreinte, signature."""
    up = update_of(config(slug, installed, uuid, email))
    if not up:
        return None
    r = httpx.get(API + up["artifact_url"], follow_redirects=True, timeout=60)
    r.raise_for_status()
    pkg = r.content
    z = zipfile.ZipFile(io.BytesIO(pkg))
    names = z.namelist()
    pkg_trust = json.loads(z.read("dm-trust.json")) if "dm-trust.json" in names else None
    out = {"target": up["target_version"], "pkg": pkg, "names": names, "trust": pkg_trust, "update": up,
           "sha_ok": up["checksum"] == "sha256:" + hashlib.sha256(pkg).hexdigest()}
    if up.get("signature") and (trust or pkg_trust):
        out["verdict"] = node_verify(pkg, up["signature"], trust or pkg_trust, up["target_version"], slug)
    return out

section("0. Préparation")
psql("INSERT INTO plugins (slug, name, device_type, status) VALUES ('banc-tb','Banc Thunderbird','matisse','active'),('banc-lo','Banc LibreOffice','libreoffice','active') ON CONFLICT DO NOTHING")
check("plugins créés", psql("SELECT count(*) FROM plugins WHERE slug LIKE 'banc-%'") == "2")

section("1. Refus à l'import (politique require)")
r = deploy(ADMIN, "banc-tb", "banc-tb-0.24.1.xpi")
check("sans signature : 422 signature_required", r.status_code == 422 and r.json().get("code") == "signature_required", r.text[:200])
r = deploy(ADMIN, "banc-tb", "banc-tb-0.24.1.xpi", "banc-tb-0.24.1.xpi.intrus.sig.json")
check("signé par une clé inconnue : 422", r.status_code == 422, r.text[:200])
r = deploy(ADMIN, "banc-tb", ("banc-tb-0.24.1.xpi", f("banc-tb-0.24.1.altere.xpi")), "banc-tb-0.24.1.xpi.sig.json")
check("paquet altéré après signature : 422 signature_invalid", r.status_code == 422 and r.json().get("code") == "signature_invalid", r.text[:200])
r = deploy(ADMIN, "banc-lo", ("banc-lo-0.24.1.xpi", f("banc-tb-0.24.1.xpi")), "banc-tb-0.24.1.xpi.sig.json")
check("signé pour un autre plugin : 422", r.status_code == 422, r.text[:200])
r = deploy(ADMIN, "banc-tb", "banc-tb-0.24.1.xpi", "banc-tb-0.24.1.xpi.sig.json", version="0.99.0")
check("numéro saisi différent du paquet : 422", r.status_code == 422, r.text[:200])
r = deploy(ADMIN, "banc-tb", "banc-tb-0.25.0-dev.229.xpi", "banc-tb-0.25.0-dev.229.xpi.sig.json")
check("branche d'essai sans confirmation : 409 ref_not_allowed", r.status_code == 409 and r.json().get("code") == "ref_not_allowed", r.text[:200])
check("aucun artefact créé par les refus", psql("SELECT count(*) FROM artifacts") == "0", psql("SELECT version FROM artifacts"))

section("2. Version générale 0.23.0 puis 0.24.0")
for v in ("0.23.0", "0.24.0"):
    r = deploy(ADMIN, "banc-tb", f"banc-tb-{v}.xpi", f"banc-tb-{v}.xpi.sig.json", general="1")
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check(f"{v} importée (201)", r.status_code == 201, r.text[:300])
    check(f"{v} origine vérifiée", (j.get("provenance") or {}).get("build", {}).get("status") == "verified", json.dumps(j.get("provenance"))[:300])
    check(f"{v} signée par le DM", j.get("signed") is True)
    check(f"{v} liste de DM réécrite", (j.get("bootstrap") or {}).get("status") == "rewritten", json.dumps(j.get("bootstrap"))[:200])

tpl = json.loads(psql("SELECT config_template FROM plugins WHERE slug='banc-tb'") or "{}")
check("config : valeur Keycloak en dur du paquet remplacée par le repère du DM", (tpl.get("prod") or {}).get("keycloakClientId") == "${{KEYCLOAK_CLIENT_ID}}", json.dumps(tpl.get("prod"))[:200])
check("config : l'import le dit", any("remplacée" in w for w in j.get("warnings", [])), json.dumps(j.get("warnings"))[:300])
check("config : profil local intact", (tpl.get("local") or {}).get("llmToken") == "exemple")
r = deploy(ADMIN, "banc-tb", "banc-tb-0.24.0.xpi", "banc-tb-0.24.0.xpi.sig.json", general="1")
check("réimport du même paquet : accepté, même empreinte", r.status_code == 201 and r.json().get("checksum") == j.get("checksum"), r.text[:200])

r = deploy(ADMIN, "banc-lo", ("banc-lo-0.24.2.xpi", f("banc-tb-0.24.2.xpi")), "banc-tb-0.24.2.xpi.sig.json")
check("nom signé inconnu du catalogue : 422", r.status_code == 422, r.text[:200])
psql("INSERT INTO plugin_aliases (alias, plugin_id) SELECT 'nom-du-depot', id FROM plugins WHERE slug='banc-tb' ON CONFLICT DO NOTHING")
pv = httpx.post(f"{ADMIN}/admin/api/deploy/extract-version", files={"binary": ("banc-tb-0.24.2.xpi", f("banc-tb-0.24.2.xpi")), "signature": ("s.json", f("banc-tb-0.24.2.xpi.sig.json"))},
                data={"plugin_id": psql("SELECT id FROM plugins WHERE slug='banc-tb'")}, timeout=30).json()
check("nom signé = alias du plugin : origine vérifiée à l'aperçu", ((pv.get("package") or {}).get("build") or {}).get("status") == "verified", json.dumps(pv.get("package"))[:300])

section("3. Poste en 0.23.0 : directive, paquet, signature")
c = client_update("banc-tb", "0.23.0", "uuid-parc-1")
check("directive vers 0.24.0", c and c["target"] == "0.24.0", str(c and c["target"]))
if c:
    check("empreinte du paquet téléchargé = directive", c["sha_ok"])
    check("la directive porte une signature", bool(c["update"].get("signature")))
    check("dm-trust.json présent dans le paquet", c["trust"] is not None)
    DELEGATED = KEYS["mode"] == "delegation"
    anchor = KEYS["root_keyid"] if DELEGATED else KEYS["dm_keyid"]
    check("ancre = " + ("racine de l'organisation" if DELEGATED else "clé du DM (mode simple)"),
          c["trust"] and [r["keyid"] for r in c["trust"]["roots"]] == [anchor], json.dumps(c["trust"])[:200])
    check("enveloppe " + ("avec" if DELEGATED else "sans") + " délégation", ("delegation" in c["update"]["signature"]) == DELEGATED)
    check("vérification WebCrypto : ok", c.get("verdict") == "ok", str(c.get("verdict")))
    prefs = zipfile.ZipFile(io.BytesIO(c["pkg"])).read("defaults/preferences/prefs.js").decode()
    check("liste de DM = cette instance", "http://localhost:18090" in prefs and "example.invalid" not in prefs, prefs[:120])
    check("autre préférence intacte", "'extensions.Banc.autre', 'inchangé'" in prefs)
    check("dm-config.json et dm-manifest.json retirés", not any(n.startswith("dm-") and n != "dm-trust.json" for n in c["names"]), str(c["names"]))
    sig, tr = c["update"]["signature"], c["trust"]
    if os.path.exists(PLUGIN_VERIFIER):
        check("module du plugin : accepte, sans connaître le nom au catalogue", plugin_verify(c["pkg"], sig, tr, "0.24.0") == "ok", plugin_verify(c["pkg"], sig, tr, "0.24.0"))
        check("module du plugin : autre extension refusée", plugin_verify(c["pkg"], sig, tr, "0.24.0", extension_id="autre@example.invalid") == "extension_mismatch")
        check("module du plugin : autre version refusée", plugin_verify(c["pkg"], sig, tr, "0.25.0") == "version_mismatch")
        if DELEGATED:
            check("module du plugin : délégation expirée refusée",
                  plugin_verify(c["pkg"], sig, tr, "0.24.0", now="2030-01-01T00:00:00Z") == "delegation_invalid")
        altered = bytearray(c["pkg"])
        altered[len(altered)//2] ^= 1
        check("module du plugin : paquet modifié refusé", plugin_verify(bytes(altered), sig, tr, "0.24.0") == "digest_mismatch")
        intruder_trust = {"schema": "dm-trust/1", "roots": [{"keyid": "0" * 16, "alg": "ES256", "public_key": tr["roots"][0]["public_key"]}]}
        check("module du plugin : ancre incohérente refusée", plugin_verify(c["pkg"], sig, intruder_trust, "0.24.0") != "ok")
    else:
        print("     (module du plugin absent : PLUGIN_VERIFIER non fourni, contrôles sautés)")
    bad = bytearray(c["pkg"])
    bad[len(bad)//2] ^= 1
    check("paquet modifié en transit : refusé", node_verify(bytes(bad), c["update"]["signature"], c["trust"], "0.24.0", "banc-tb") == "digest_mismatch")
    check("signature rejouée pour une autre version : refusée", node_verify(c["pkg"], c["update"]["signature"], c["trust"], "0.25.0", "banc-tb") == "version_mismatch")
    if DELEGATED:
        check("délégation expirée : refusée", node_verify(c["pkg"], c["update"]["signature"], c["trust"],
              "0.24.0", "banc-tb", "2030-01-01T00:00:00Z").startswith("delegation_invalid"))
    st = json.loads(__import__("base64").b64decode(c["update"]["signature"]["payload"]))
    check("déclaration : identifiant de l'extension", st.get("extension_id") == "banc@example.invalid", str(st.get("extension_id")))
    check("déclaration : paquet d'origine vérifié et fichiers modifiés listés", st["upstream"].get("verified") is True and "dm-trust.json" in st["changed_files"] and "dm-config.json" in st["changed_files"], json.dumps(st)[:300])
    r = httpx.get(f"{API}/catalog/banc-tb/download/banc-tb-0.24.0.xpi.sig.json")
    check("enveloppe servie au catalogue", r.status_code == 200 and r.json().get("schema") == "dm-package-signature/1", r.text[:120])
c = client_update("banc-tb", "0.24.0", "uuid-parc-2")
check("poste à jour : pas de directive", c is None)

section("4. Le pod de service n'a pas la clé")
r = deploy(API, "banc-tb", "banc-tb-0.24.1.xpi", "banc-tb-0.24.1.xpi.sig.json")
check("import par le pod API refusé : 422 signing_config", r.status_code == 422 and r.json().get("code") == "signing_config", r.text[:200])


section("5. Cohabitation : générale, candidate, deux essais")
PID = psql("SELECT id FROM plugins WHERE slug='banc-tb'")
def admin_upload(pkg, sig, version, **fields):
    files = {"binary": (pkg, f(pkg), "application/octet-stream")}
    if sig:
        files["signature"] = (sig, f(sig), "application/json")
    data = {"version": version, "action": "deploy", "deploy_strategy": "patch_all", "environment": "banc", **fields}
    return httpx.post(f"{ADMIN}/admin/catalog/{PID}/versions/upload", files=files, data=data,
                      headers={"accept": "application/json"}, follow_redirects=False, timeout=60)
COHORTS = {"0.25.0-rc.241": "pilote@banc.test", "0.25.0-dev.229": "recherche@banc.test", "0.25.0-dev.236": "agent@banc.test"}
r = admin_upload("banc-tb-0.25.0-rc.241.xpi", "banc-tb-0.25.0-rc.241.xpi.sig.json", "0.25.0-rc.241",
                 campaign_kind="experiment", target_mode="emails", emails=COHORTS["0.25.0-rc.241"], tag="rc")
check("candidate (develop, référence acceptée) : importée", r.status_code in (200, 302, 303), f"{r.status_code} {r.text[:300]}")
for v, tag in (("0.25.0-dev.229", "exp-recherche-v2"), ("0.25.0-dev.236", "exp-chat-agent")):
    r = admin_upload(f"banc-tb-{v}.xpi", f"banc-tb-{v}.xpi.sig.json", v, campaign_kind="experiment",
                     target_mode="emails", emails=COHORTS[v], tag=tag, hypotheses="Question 1\nQuestion 2")
    check(f"essai {v} sans confirmation : 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    r = admin_upload(f"banc-tb-{v}.xpi", f"banc-tb-{v}.xpi.sig.json", v, campaign_kind="experiment",
                     target_mode="emails", emails=COHORTS[v], tag=tag, hypotheses="Question 1\nQuestion 2", confirm_ref="1")
    check(f"essai {v} confirmé : importé", r.status_code in (200, 302, 303), f"{r.status_code} {r.text[:300]}")
check("confirmation tracée au journal d'audit", int(psql("SELECT count(*) FROM admin_audit_log WHERE payload::text LIKE '%ref_confirmed%'") or 0) >= 2,
      psql("SELECT action, left(payload::text,300) FROM admin_audit_log ORDER BY id DESC LIMIT 3"))
WHO = {"parc": ("uuid-parc-9", "", "0.24.0"), "pilote": ("uuid-pilote", "pilote@banc.test", "0.25.0-rc.241"),
       "recherche": ("uuid-rech", "recherche@banc.test", "0.25.0-dev.229"), "agent": ("uuid-agent", "agent@banc.test", "0.25.0-dev.236")}
for who, (uuid, email, expected) in WHO.items():
    c = client_update("banc-tb", "0.23.0", uuid, email)
    check(f"poste « {who} » reçoit {expected}", c and c["target"] == expected, str(c and c["target"]))
    if c:
        check(f"poste « {who} » : signature vérifiée", c.get("verdict") == "ok" and c["sha_ok"], str(c.get("verdict")))
r = httpx.get(f"{API}/catalog/banc-tb/download", follow_redirects=False)
check("téléchargement sans lien = générale 0.24.0", "banc-tb-0.24.0" in r.headers.get("location", ""), r.headers.get("location", ""))
r = httpx.get(f"{API}/catalog/banc-tb/download?tag=exp-recherche-v2", follow_redirects=False)
check("lien d'essai = 0.25.0-dev.229", "0.25.0-dev.229" in r.headers.get("location", ""), r.headers.get("location", ""))
r = httpx.get(f"{API}/catalog/banc-tb")
check("catalogue sans lien : aucun essai visible", "dev.229" not in r.text and "dev.236" not in r.text)
r = httpx.get(f"{API}/catalog/banc-tb?exp=exp-recherche-v2")
check("catalogue avec lien : l'essai et ses questions", "0.25.0-dev.229" in r.text and "Question 1" in r.text and "dev.236" not in r.text)

section("6. Sortie de la finale 0.25.0")
r = deploy(ADMIN, "banc-tb", "banc-tb-0.25.0.xpi", "banc-tb-0.25.0.xpi.sig.json", general="1")
check("0.25.0 importée", r.status_code == 201, r.text[:200])
c = client_update("banc-tb", "0.24.0", "uuid-parc-9")
check("parc : 0.24.0 → 0.25.0", c and c["target"] == "0.25.0", str(c and c["target"]))
c = client_update("banc-tb", "0.25.0-rc.241", "uuid-pilote", "pilote@banc.test")
check("pilote déjà en rc.241 : rien tant que son essai est actif", c is None, str(c and c["target"]))
psql(f"UPDATE campaigns SET status='completed' WHERE is_experiment AND plugin_id={int(PID)}")
for who, installed, email in (("pilote", "0.25.0-rc.241", "pilote@banc.test"), ("recherche", "0.25.0-dev.229", "recherche@banc.test")):
    c = client_update("banc-tb", installed, "uuid-" + who, email)
    check(f"essais terminés : « {who} » ({installed}) reçoit la finale 0.25.0", c and c["target"] == "0.25.0", str(c and c["target"]))

section("7. LibreOffice")
r = deploy(ADMIN, "banc-lo", "banc-lo-0.3.1.oxt", "banc-lo-0.3.1.oxt.sig.json", general="1")
check("OXT d'une branche hors liste (master) : 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
r = deploy(ADMIN, "banc-lo", "banc-lo-0.3.1.oxt", "banc-lo-0.3.1.oxt.sig.json", general="1", confirm_ref="1")
check("OXT confirmé : importé et signé", r.status_code == 201 and r.json().get("signed"), r.text[:200])
c = client_update("banc-lo", "0.3.0", "uuid-lo-1")
check("OXT : directive, empreinte et signature", c and c["target"] == "0.3.1" and c["sha_ok"] and c.get("verdict") == "ok", str(c and (c["target"], c.get("verdict"))))
if c:
    z = zipfile.ZipFile(io.BytesIO(c["pkg"]))
    check("OXT : config et flux natif réécrits", "localhost:18090" in z.read("config.default.json").decode() and "localhost:18090" in z.read("description.xml").decode())

section("8. Import depuis les releases GitHub (faux serveur)")
source = {"repository": "IA-Generative/banc-plugin", "component": "banc", "asset_pattern": "banc-tb-*.xpi", "channel": "general"}
psql(f"UPDATE plugins SET github_source = '{json.dumps(source)}'::jsonb WHERE id = {int(PID)}")
r = httpx.post(f"{ADMIN}/admin/catalog/{PID}/github/check", follow_redirects=False, timeout=30)
check("« Vérifier maintenant » accepté", r.status_code in (200, 303), f"{r.status_code} {r.text[:200]}")
for _ in range(40):
    if psql(f"SELECT count(*) FROM plugin_github_releases WHERE plugin_id = {int(PID)}") == "2":
        break
    __import__("time").sleep(1)
rows = dict(line.split("|") for line in psql(
    f"SELECT version, state FROM plugin_github_releases WHERE plugin_id = {int(PID)} ORDER BY version").splitlines())
check("relevé par le worker : générale disponible, candidate hors canal", rows == {"0.27.0": "available", "0.27.1-rc.5": "ignored"}, str(rows))
r = httpx.post(f"{ADMIN}/admin/catalog/{PID}/github/import", json={"tag": "banc-v0.27.0"}, timeout=60)
j = r.json() if r.status_code == 200 else {}
check("paquet et signature tirés côté serveur", r.status_code == 200 and j.get("upload_id") and j.get("signature") and not j.get("refusal"), r.text[:300])
r = httpx.post(f"{ADMIN}/admin/catalog/{PID}/versions/upload", follow_redirects=False, timeout=60,
               json={"version": "0.27.0", "upload_id": j.get("upload_id"), "signature": j.get("signature"), "action": "publish_only"})
check("envoi par l'assistant avec le dépôt tiré de GitHub", r.status_code == 303, f"{r.status_code} {r.text[:300]}")
prov = json.loads(psql("SELECT provenance FROM artifacts WHERE version = '0.27.0'") or "{}")
check("provenance : origine vérifiée et release GitHub", (prov.get("build") or {}).get("status") == "verified"
      and (prov.get("github") or {}).get("tag") == "banc-v0.27.0", json.dumps(prov)[:300])
check("relevé : release marquée importée", psql(
    f"SELECT state FROM plugin_github_releases WHERE plugin_id = {int(PID)} AND version = '0.27.0'") == "imported")
check("aucune campagne créée par l'import", psql(
    "SELECT count(*) FROM campaigns c JOIN artifacts a ON a.id = c.artifact_id WHERE a.version = '0.27.0'") == "0")

print(f"\n{sum(1 for _, ok, _ in RES if ok)}/{len(RES)} contrôles passés")
sys.exit(0 if all(ok for _, ok, _ in RES) else 1)
