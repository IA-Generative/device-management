"""Cas hérité : le cache du pod API est DÉJÀ périmé (divergence créée sous
l'ancienne image), et le pod vient d'être remplacé par la version corrigée.
Un seul téléchargement doit suffire à le réparer."""
import hashlib
import os

import httpx
import psycopg2

TOK = os.environ["DM_QUEUE_ADMIN_TOKEN"]
API = "http://dm-api"
SLUG = "dm-issue5-probe"
conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT checksum FROM artifacts WHERE device_type='libreoffice' AND version='1.0.0' ORDER BY id DESC LIMIT 1")
attendu = cur.fetchone()[0]
print("checksum en base (binaire B de l'étape AVANT) :", attendu)
r = httpx.get(f"{API}/api/files", headers={"x-admin-token": TOK}, timeout=30)
print("cache du pod API hérité du PVC :", r.text[:200])
r = httpx.get(f"{API}/catalog/{SLUG}/download", follow_redirects=True, timeout=180)
obtenu = "sha256:" + hashlib.sha256(r.content).hexdigest()
print(f"téléchargement : HTTP {r.status_code}  {len(r.content)} octets → {obtenu}")
print("VERDICT :", "CONCORDANT — cache périmé réparé au service" if obtenu == attendu else f"MISMATCH attendu={attendu}")
