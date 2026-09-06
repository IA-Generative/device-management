"""Deux répliques d'API, deux caches indépendants : chacune doit se réparer
seule au premier téléchargement qui suit le ré-upload — sans qu'aucune
invalidation ne circule entre elles."""
import hashlib
import os
import sys

import httpx
import psycopg2

TOK = os.environ["DM_QUEUE_ADMIN_TOKEN"]
ADMIN = "http://dm-admin"
SLUG = "dm-issue5-probe"
V = "1.0.0"
REPLIQUES = {"réplique 1 (PVC)": "http://dm-api", "réplique 2 (disque propre)": "http://dm-api2"}


def sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

def publier(data, quoi):
    r = httpx.post(
        f"{ADMIN}/api/plugins/{SLUG}/deploy",
        headers={"x-admin-token": TOK},
        files={"binary": (f"{SLUG}-{V}.oxt", data, "application/octet-stream")},
        data={"version": V, "strategy": "immediate"},
        timeout=180,
    )
    print(f"publication {quoi} : HTTP {r.status_code} checksum={r.json().get('checksum')}")


def tele(url):
    r = httpx.get(f"{url}/catalog/{SLUG}/download", follow_redirects=True, timeout=180)
    return r.status_code, sha(r.content)


A, B = os.urandom(65536), os.urandom(65536)
print(f"A = {sha(A)}\nB = {sha(B)}")

print("\n=== 1. Publication de A, puis un téléchargement sur CHAQUE réplique (les deux caches se peuplent)")
publier(A, "A")
for nom, url in REPLIQUES.items():
    st, h = tele(url)
    print(f"   {nom:28s} HTTP {st}  {h}  {'= A' if h == sha(A) else '≠ A'}")

print(f"\n=== 2. Ré-upload de B sous le MÊME numéro ({V})")
publier(B, "B")
cur.execute(
    "SELECT checksum FROM artifacts WHERE device_type='libreoffice' AND version=%s "
    "ORDER BY id DESC LIMIT 1", (V,))
attendu = cur.fetchone()[0]
print(f"checksum en base : {attendu}")

print("\n=== 3. Un téléchargement sur chaque réplique — aucune invalidation n'a circulé")
ok = True
for nom, url in REPLIQUES.items():
    st, h = tele(url)
    concord = h == attendu
    ok &= concord
    print(f"   {nom:28s} HTTP {st}  {h}  {'CONCORDANT' if concord else 'MISMATCH'}")
print("\nVERDICT :", "les DEUX répliques servent B — chacune s'est réparée seule." if ok
      else "au moins une réplique sert encore un binaire périmé.")
sys.exit(0 if ok else 1)
