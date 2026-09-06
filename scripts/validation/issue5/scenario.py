"""Scénario issue #5 sur Kubernetes — ré-upload d'une même version.

Publie A sur le pod ADMIN, télécharge par le pod API (peuple son cache PVC),
republie B SOUS LE MÊME NUMÉRO, retélécharge. Compare les octets servis au
checksum en base — le contrôle que fait le plugin avant d'installer.
"""
import hashlib
import os
import sys

import httpx
import psycopg2

TOK = os.environ["DM_QUEUE_ADMIN_TOKEN"]
ADMIN = "http://dm-admin"
API = "http://dm-api"
SLUG = "dm-issue5-probe"
VERSION = "1.0.0"


def sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def say(t):
    print(f"\n=== {t}", flush=True)


conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

say("0. Réinitialisation (plugin de sonde, artefacts, caches des deux pods)")
cur.execute("SELECT id FROM plugins WHERE slug = %s", (SLUG,))
for (pid,) in cur.fetchall():
    cur.execute("DELETE FROM campaigns WHERE plugin_id = %s", (pid,))
    cur.execute("DELETE FROM plugin_versions WHERE plugin_id = %s", (pid,))
    cur.execute("DELETE FROM plugins WHERE id = %s", (pid,))
cur.execute("DELETE FROM artifacts WHERE device_type = 'libreoffice' AND version = %s", (VERSION,))
cur.execute(
    "INSERT INTO plugins (slug, name, device_type, status, visibility) "
    "VALUES (%s, 'Sonde issue #5', 'libreoffice', 'active', 'hidden') RETURNING id",
    (SLUG,),
)
print(f"plugin de sonde créé (id={cur.fetchone()[0]}, visibility=hidden)")
for base, label in ((ADMIN, "admin"), (API, "api")):
    r = httpx.get(f"{base}/api/files", headers={"x-admin-token": TOK}, timeout=30)
    print(f"cache {label} avant : {r.status_code} {r.text[:200]}")

A, B = os.urandom(65536), os.urandom(65536)
print(f"sha256(binaire A) = {sha(A)}")
print(f"sha256(binaire B) = {sha(B)}")


def publier(data, quoi):
    r = httpx.post(
        f"{ADMIN}/api/plugins/{SLUG}/deploy",
        headers={"x-admin-token": TOK},
        files={"binary": (f"{SLUG}-{VERSION}.oxt", data, "application/octet-stream")},
        data={"version": VERSION, "strategy": "immediate"},
        timeout=180,
    )
    print(f"publication {quoi} : HTTP {r.status_code} {r.text[:260]}")
    return r


def telecharger(n):
    r = httpx.get(f"{API}/catalog/{SLUG}/download", follow_redirects=True, timeout=180)
    print(f"téléchargement {n} : HTTP {r.status_code}  {len(r.content)} octets  → {sha(r.content)}")
    return r.content


def checksum_en_base():
    cur.execute(
        "SELECT checksum FROM artifacts WHERE device_type='libreoffice' AND version=%s "
        "ORDER BY id DESC LIMIT 1", (VERSION,))
    row = cur.fetchone()
    return row[0] if row else None


say(f"1. Publication du binaire A en {VERSION} (pod ADMIN, porteur de son disque)")
publier(A, "A")

say("2. Premier téléchargement sur le pod API — peuple son cache PVC")
dl1 = telecharger(1)
print(f"checksum en base : {checksum_en_base()}")
r = httpx.get(f"{API}/api/files", headers={"x-admin-token": TOK}, timeout=30)
print(f"cache du pod API : {r.text[:300]}")

say(f"3. Ré-upload du binaire B SOUS LE MÊME NUMÉRO ({VERSION})")
publier(B, "B")

say("4. Second téléchargement sur le pod API")
dl2 = telecharger(2)

say("VERDICT")
attendu = checksum_en_base()
obtenu = sha(dl2)
print(f"checksum en base (ce que /config annonce) : {attendu}")
print(f"sha256 des octets servis par /catalog     : {obtenu}")
print(f"  (A = {sha(A)})")
print(f"  (B = {sha(B)})")
if obtenu == attendu:
    print("RESULTAT : CONCORDANT — le binaire servi est bien celui annoncé.")
    code = 0
else:
    print(f"RESULTAT : CHECKSUM MISMATCH — expected={attendu} actual={obtenu}")
    if obtenu == sha(A):
        print("           les octets servis sont ceux de A (cache périmé du pod API).")
    code = 1

say("5. Cas nominal — 3 téléchargements de plus sur un cache désormais conforme")
for i in range(3, 6):
    telecharger(i)

sys.exit(code)
