#!/usr/bin/env python3
"""E2E — capacité « plusieurs versions d'un plugin en parallèle » (0.9.14+).

Vérifie de bout en bout, contre un environnement DÉPLOYÉ, les trois mécanismes :
push de campagnes coexistantes par cohorte, pull de versions expérimentales au
catalogue, et cycle de vie du cache binaire. Là où les tests d'intégration
(`tests/test_experiment_campaigns_pg.py`) prouvent le SQL, celui-ci prouve la
chaîne complète — jusqu'à comparer l'octet servi au binaire attendu.

⚠️ ÉCRIT DANS UN ENVIRONNEMENT RÉEL. À réserver à l'intégration, jamais la prod.
Isolation : tout est préfixé `e2e-par`, et le device_type dédié `e2e-par-plugin`
garantit qu'aucune campagne créée ici ne peut atteindre un vrai device (scoping
par plugin, 0.9.13). Base ET fichiers sont nettoyés en sortie, y compris sur
erreur ; `--keep` conserve les données pour inspection.

Prérequis :
  - un contexte kubectl pointant l'environnement cible, namespace `bootstrap`
    (les pods `postgres`, `device-management` et `device-management-admin`
    doivent y répondre) ;
  - un accès HTTP au service, typiquement par port-forward.

Usage :
    kubectl port-forward -n bootstrap svc/device-management 18089:80 &
    python3 scripts/e2e-parallel-versions.py http://localhost:18089
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18089"
KEEP = "--keep" in sys.argv
NS = "bootstrap"
SLUG = "e2e-par-plugin"
DEVICE_TYPE = "e2e-par-plugin"
COHORT = "E2E-PAR testeurs"
COHORT_B = "E2E-PAR testeurs B"
EMAIL_IN = "e2e-par-testeur@example.test"
EMAIL_OUT = "e2e-par-temoin@example.test"
UUID_IN = "e2e0aaaa-0000-4000-8000-000000000001"
UUID_OUT = "e2e0bbbb-0000-4000-8000-000000000002"
TAG_EXP = "e2e-exp"
TAG_EXP2 = "e2e-exp-b"

V_STABLE, V_RC, V_EXP, V_NEXT = "1.5.0", "1.6.0-rc1", "1.7.0-exp", "1.8.0"
V_EXP2 = "1.7.0-exp-b"   # seconde branche expérimentale, réutilise l'artefact 1.8.0

G, R, Y, B, N = "\033[0;32m", "\033[0;31m", "\033[0;33m", "\033[0;34m", "\033[0m"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    mark = f"{G}✓{N}" if cond else f"{R}✗{N}"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{B}▶ {title}{N}")


def psql(sql: str) -> str:
    """Exécute du SQL dans le pod postgres et renvoie la sortie brute."""
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, "deploy/postgres", "--",
         "psql", "-U", "postgres", "-d", "bootstrap", "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"psql a échoué: {out.stderr.strip()[:400]}")
    return out.stdout.strip()


def scalar(sql: str) -> str:
    out = psql(sql)          # une seule exécution : le SQL n'est pas idempotent
    return out.splitlines()[0].strip() if out else ""


def put_binary(rel_path: str, content: bytes) -> None:
    """Pose un binaire sur le PVC admin (source de vérité en mode local).

    Écriture directe via python3 dans le pod : le conteneur n'embarque pas curl,
    et le PVC est exactement l'emplacement où l'API d'upload aurait écrit.
    """
    import base64 as _b64
    payload = _b64.b64encode(content).decode()
    prog = (
        "import base64,os,sys;"
        "p=os.path.join(os.environ.get('DM_LOCAL_BINARIES_DIR') or '/data/content/binaries', sys.argv[1]);"
        "os.makedirs(os.path.dirname(p), exist_ok=True);"
        "open(p,'wb').write(base64.b64decode(sys.argv[2]));"
        "print('OK', p, os.path.getsize(p))"
    )
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, "deploy/device-management-admin", "--",
         "python3", "-c", prog, rel_path, payload],
        capture_output=True, text=True)
    if "OK" not in out.stdout:
        raise RuntimeError(f"upload {rel_path} : {out.stderr.strip()[:300]}")


def pod_post(deploy: str, path: str) -> str:
    """POST authentifié depuis un pod (le token admin ne quitte pas le cluster)."""
    prog = (
        "import os,urllib.request,urllib.error;"
        "req=urllib.request.Request('http://localhost:'+os.environ.get('DM_PORT','3001')+"
        f"'{path}', method='POST');"
        "req.add_header('x-admin-token', os.environ['DM_QUEUE_ADMIN_TOKEN']);"
        "\ntry:\n"
        "    r=urllib.request.urlopen(req, timeout=30); print(r.status, r.read()[:200].decode())\n"
        "except urllib.error.HTTPError as e: print(e.code, e.read()[:200].decode())"
    )
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, f"deploy/{deploy}", "--", "python3", "-c", prog],
        capture_output=True, text=True)
    return (out.stdout or out.stderr).strip()


def pod_post_json(deploy: str, path: str, payload: dict) -> dict:
    """POST JSON authentifié depuis un pod. Sert à exercer les VRAIS chemins de
    l'application (l'auto-complétion vit dans le service, pas dans du SQL brut)."""
    prog = f'''
import json, os, urllib.request, urllib.error
body = json.dumps({json.dumps(payload)}).encode()
req = urllib.request.Request(
    "http://localhost:" + os.environ.get("DM_PORT", "3001") + {path!r},
    data=body, method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("X-Admin-Token", os.environ["DM_QUEUE_ADMIN_TOKEN"])
try:
    r = urllib.request.urlopen(req, timeout=30)
    print(json.dumps({{"status": r.status, **json.loads(r.read() or b"{{}}")}}))
except urllib.error.HTTPError as e:
    print(json.dumps({{"status": e.code, "error": e.read()[:200].decode()}}))
'''
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, f"deploy/{deploy}", "--", "python3", "-c", prog],
        capture_output=True, text=True)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"status": 0, "error": (out.stdout + out.stderr)[:300]}


def http(path: str, headers: dict | None = None, method: str = "GET", follow: bool = True):
    """Renvoie (status, corps_bytes, headers). follow=False pour voir les 302."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(*( [] if follow else [NoRedirect] ))
    req = urllib.request.Request(BASE + path, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def config_for(email: str, uuid: str, version: str) -> dict:
    st, body, _ = http(f"/config/{SLUG}/config.json", {
        "X-Plugin-Version": version,
        "X-Client-UUID": uuid,
        "X-User-Email": email,
    })
    if st != 200:
        raise RuntimeError(f"/config → {st}: {body[:200]!r}")
    return json.loads(body)


def directive(cfg: dict) -> dict | None:
    for key in ("update", "update_directive", "updateDirective"):
        if isinstance(cfg.get(key), dict):
            return cfg[key]
    return None


# ── Données ────────────────────────────────────────────────────────────────

BIN = {
    V_STABLE: b"E2E-PAR stable  " + V_STABLE.encode() + b"\n" + b"S" * 512,
    V_RC:     b"E2E-PAR rc      " + V_RC.encode() + b"\n" + b"R" * 512,
    V_EXP:    b"E2E-PAR exp     " + V_EXP.encode() + b"\n" + b"X" * 512,
    V_NEXT:   b"E2E-PAR next    " + V_NEXT.encode() + b"\n" + b"N" * 512,
}
SHA = {v: "sha256:" + hashlib.sha256(c).hexdigest() for v, c in BIN.items()}
RELPATH = {v: f"{DEVICE_TYPE}/{v}_plugin.oxt" for v in BIN}
# artifacts.s3_path est ABSOLU en mode local (convention constatée en base).
ABSPATH = {v: f"/data/content/binaries/{RELPATH[v]}" for v in BIN}


def cleanup() -> None:
    """Retire les données ET les fichiers. Le premier jet ne nettoyait que la
    base : les binaires restaient sur les deux PVC après le run."""
    for deploy in ("device-management-admin", "device-management"):
        subprocess.run(
            ["kubectl", "exec", "-n", NS, f"deploy/{deploy}", "--", "python3", "-c",
             "import os,shutil;"
             "shutil.rmtree(os.path.join("
             "os.environ.get('DM_LOCAL_BINARIES_DIR') or '/data/content/binaries',"
             f"'{DEVICE_TYPE}'), ignore_errors=True)"],
            capture_output=True, text=True)
    psql(f"""
        DELETE FROM campaigns WHERE name LIKE 'E2E-PAR%';
        DELETE FROM plugin_versions WHERE plugin_id IN (SELECT id FROM plugins WHERE slug='{SLUG}');
        DELETE FROM cohort_members WHERE cohort_id IN (SELECT id FROM cohorts WHERE name LIKE 'E2E-PAR%');
        DELETE FROM cohorts WHERE name LIKE 'E2E-PAR%';
        DELETE FROM artifacts WHERE device_type='{DEVICE_TYPE}';
        DELETE FROM plugins WHERE slug='{SLUG}';
    """)


def seed() -> dict:
    cleanup()
    ids = {}
    ids["plugin"] = scalar(f"""
        INSERT INTO plugins (slug, name, device_type, status)
        VALUES ('{SLUG}', 'E2E-PAR plugin', '{DEVICE_TYPE}', 'active') RETURNING id""")
    for v in BIN:
        ids[f"art_{v}"] = scalar(f"""
            INSERT INTO artifacts (device_type, version, s3_path, checksum, is_active)
            VALUES ('{DEVICE_TYPE}', '{v}', '{ABSPATH[v]}', '{SHA[v]}', true) RETURNING id""")
    for name, key in ((COHORT, "cohort"), (COHORT_B, "cohort_b")):
        ids[key] = scalar(f"""
            INSERT INTO cohorts (name, type, config)
            VALUES ('{name}', 'manual', '{{}}'::jsonb) RETURNING id""")
    psql(f"""INSERT INTO cohort_members (cohort_id, identifier_type, identifier_value)
             VALUES ({ids['cohort']}, 'email', '{EMAIL_IN}'),
                    ({ids['cohort']}, 'client_uuid', '{UUID_IN}')""")
    # Catalogue : la main publiée + une branche expérimentale taguée.
    psql(f"""INSERT INTO plugin_versions (plugin_id, version, artifact_id, status, published_at)
             VALUES ({ids['plugin']}, '{V_STABLE}', {ids[f'art_{V_STABLE}']}, 'published', NOW())""")
    psql(f"""INSERT INTO plugin_versions (plugin_id, version, artifact_id, status, tag, hypotheses, published_at)
             VALUES ({ids['plugin']}, '{V_EXP}', {ids[f'art_{V_EXP}']}, 'experimental',
                     '{TAG_EXP}', '["Le resume tient-il sur 3 lignes ?", "Latence acceptable ?"]'::jsonb, NOW())""")
    # La RC push est aussi une version experimental (servie par la route versionnée).
    psql(f"""INSERT INTO plugin_versions (plugin_id, version, artifact_id, status, tag, published_at)
             VALUES ({ids['plugin']}, '{V_RC}', {ids[f'art_{V_RC}']}, 'experimental', 'rc', NOW())""")
    for v in BIN:
        put_binary(RELPATH[v], BIN[v])
    return ids


def campaign(name, artifact_version, ids, *, is_exp=False, cohort=None, priority=0, status="active"):
    coh = "NULL" if cohort is None else str(cohort)
    return scalar(f"""
        INSERT INTO campaigns (name, type, status, plugin_id, artifact_id, target_cohort_id,
                               is_experiment, priority, urgency, created_by)
        VALUES ('{name}', 'plugin_update', '{status}', {ids['plugin']},
                {ids[f'art_{artifact_version}']}, {coh}, {str(is_exp).lower()}, {priority},
                'normal', 'e2e') RETURNING id""")


def status_of(cid: str) -> str:
    return scalar(f"SELECT status FROM campaigns WHERE id={cid}")


# ── Scénario ───────────────────────────────────────────────────────────────

def main() -> int:
    print(f"{B}=== E2E versions parallèles — cible {BASE} ==={N}")
    st, body, _ = http("/healthz")
    if st != 200:
        print(f"{R}Service injoignable ({st}){N}")
        return 2

    ids = seed()
    print(f"  plugin={ids['plugin']} cohorte={ids['cohort']} (données E2E-PAR isolées)")

    section("A. Push — campagnes coexistantes par cohorte")
    c_general = campaign("E2E-PAR rollout stable", V_STABLE, ids)
    c_exp = campaign("E2E-PAR bras RC", V_RC, ids, is_exp=True, cohort=ids["cohort"], priority=10)
    check("A1 coexistence : rollout général ET bras d'expé restent actifs",
          status_of(c_general) == "active" and status_of(c_exp) == "active",
          f"général={status_of(c_general)}, expé={status_of(c_exp)}")

    d_in = directive(config_for(EMAIL_IN, UUID_IN, V_STABLE))
    check("A2 device DANS la cohorte reçoit la RC",
          d_in is not None and d_in.get("target_version") == V_RC,
          f"target={d_in and d_in.get('target_version')}")
    check("A3 checksum servi = celui de la RC",
          bool(d_in) and d_in.get("checksum") == SHA[V_RC])

    url = (d_in or {}).get("artifact_url", "")
    check("A4 URL épinglée sur la version cible (correction QC)",
          V_RC in url and url != f"/catalog/{SLUG}/download", f"url={url}")

    # LE test du bug : suivre l'URL et comparer l'octet servi au binaire attendu.
    st_dl, payload, _ = http(url) if url else (0, b"", {})
    served = hashlib.sha256(payload).hexdigest() if st_dl == 200 else ""
    check("A5 l'URL sert RÉELLEMENT le binaire de la RC (et non la main)",
          st_dl == 200 and "sha256:" + served == SHA[V_RC],
          f"HTTP {st_dl}, servi={'sha256:' + served[:16] if served else '-'}…, "
          f"attendu={SHA[V_RC][:23]}…, main={SHA[V_STABLE][:23]}…")

    d_out = directive(config_for(EMAIL_OUT, UUID_OUT, "1.0.0"))
    check("A6 device HORS cohorte reste témoin sur le stable",
          d_out is not None and d_out.get("target_version") == V_STABLE,
          f"target={d_out and d_out.get('target_version')}")

    d_same = directive(config_for(EMAIL_IN, UUID_IN, V_RC))
    check("A7 device déjà sur la cible : aucune directive (pas de boucle)",
          d_same is None, f"directive={d_same}")

    api_out = pod_post_json("device-management", "/api/campaigns", {
        "name": "E2E-PAR release suivante", "type": "plugin_update", "status": "active",
        "artifact_id": int(ids[f"art_{V_NEXT}"]),
    })
    c_new = str(api_out.get("campaign_id", "0"))
    check("A8 nouvelle release générale supersede le général, épargne l'expé",
          status_of(c_general) == "completed" and status_of(c_exp) == "active"
          and status_of(c_new) == "active",
          f"ancien={status_of(c_general)}, expé={status_of(c_exp)}, neuf={status_of(c_new)}")

    d_after = directive(config_for(EMAIL_IN, UUID_IN, V_STABLE))
    check("A9 le testeur reste sur son bras malgré la nouvelle release",
          d_after is not None and d_after.get("target_version") == V_RC,
          f"target={d_after and d_after.get('target_version')}")

    c_b = campaign("E2E-PAR bras B", V_NEXT, ids, is_exp=True, cohort=ids["cohort_b"], priority=50)
    check("A10 un nouveau bras (autre cohorte) n'écrase pas le bras A",
          status_of(c_exp) == "active" and status_of(c_b) == "active")

    psql(f"""INSERT INTO cohort_members (cohort_id, identifier_type, identifier_value)
             VALUES ({ids['cohort_b']}, 'email', '{EMAIL_IN}')""")
    d_two = directive(config_for(EMAIL_IN, UUID_IN, V_STABLE))
    check("A11 device dans 2 bras : priority tranche (50 > 10)",
          d_two is not None and d_two.get("target_version") == V_NEXT,
          f"target={d_two and d_two.get('target_version')}")

    section("B. Pull — versions expérimentales au catalogue")
    st_pub, page_pub, _ = http(f"/catalog/{SLUG}")
    check("B1 page publique : la branche expé est invisible sans tag",
          st_pub == 200 and TAG_EXP.encode() not in page_pub and V_EXP.encode() not in page_pub,
          f"HTTP {st_pub}")

    st_exp, page_exp, _ = http(f"/catalog/{SLUG}?exp={TAG_EXP}")
    check("B2 avec ?exp=<tag> : section révélée, version + questions affichées",
          st_exp == 200 and V_EXP.encode() in page_exp
          and b"Le resume tient-il" in page_exp, f"HTTP {st_exp}")

    st_wrong, page_wrong, _ = http(f"/catalog/{SLUG}?exp=mauvais-tag")
    check("B3 mauvais tag : rien n'est révélé",
          st_wrong == 200 and V_EXP.encode() not in page_wrong)

    st_r, _, hdr_r = http(f"/catalog/{SLUG}/download?tag={TAG_EXP}", follow=False)
    loc = hdr_r.get("location", "")
    check("B4 /download?tag= redirige vers la branche expérimentale",
          st_r in (301, 302, 307) and V_EXP in loc, f"{st_r} → {loc}")

    st_m, _, hdr_m = http(f"/catalog/{SLUG}/download", follow=False)
    loc_m = hdr_m.get("location", "")
    check("B5 /download sans tag sert toujours la main",
          st_m in (301, 302, 307) and V_STABLE in loc_m and V_EXP not in loc_m,
          f"{st_m} → {loc_m}")

    st_f, payload_f, _ = http(f"/catalog/{SLUG}/download/{SLUG}-{V_EXP}.oxt")
    check("B6 route versionnée : sert bien le binaire expérimental",
          st_f == 200 and hashlib.sha256(payload_f).hexdigest() == SHA[V_EXP][7:],
          f"HTTP {st_f}")

    st_d, payload_d, _ = http(f"/catalog/{SLUG}/download/{SLUG}-{V_STABLE}.oxt")
    check("B7 les 3 versions coexistent et sont servies distinctement",
          st_d == 200 and hashlib.sha256(payload_d).hexdigest() == SHA[V_STABLE][7:]
          and payload_d != payload_f)

    section("C. Cycle de vie du cache binaire")
    st_l, body_l, _ = http("/api/files")
    check("C1 l'API fichiers reste protégée par token", st_l == 403, f"HTTP {st_l}")

    ev = pod_post("device-management", "/api/files/evict")
    check("C2 POST /api/files/evict répond (self-healing du cache)",
          ev.startswith("200"), ev[:120])

    st_after, payload_after, _ = http(f"/catalog/{SLUG}/download/{SLUG}-{V_EXP}.oxt")
    check("C3 après éviction, le binaire vivant est re-servi (pull-on-miss)",
          st_after == 200 and hashlib.sha256(payload_after).hexdigest() == SHA[V_EXP][7:],
          f"HTTP {st_after}")

    section("D. Cohabitation étendue — plusieurs branches sur le même plugin")
    # Une seconde branche taguée, en parallèle de la première ET de la main.
    psql(f"""INSERT INTO plugin_versions (plugin_id, version, artifact_id, status, tag, published_at)
             VALUES ({ids['plugin']}, '{V_EXP2}', {ids[f'art_{V_NEXT}']}, 'experimental',
                     '{TAG_EXP2}', NOW())""")

    st_a, _, hdr_a = http(f"/catalog/{SLUG}/download?tag={TAG_EXP}", follow=False)
    st_b, _, hdr_b = http(f"/catalog/{SLUG}/download?tag={TAG_EXP2}", follow=False)
    loc_a, loc_b = hdr_a.get("location", ""), hdr_b.get("location", "")
    check("D1 deux branches taguées coexistent, chacune sert SA version",
          V_EXP in loc_a and V_EXP2 in loc_b and loc_a != loc_b,
          f"{TAG_EXP}→{loc_a.rsplit('/', 1)[-1]}  |  {TAG_EXP2}→{loc_b.rsplit('/', 1)[-1]}")

    st_m2, _, hdr_m2 = http(f"/catalog/{SLUG}/download", follow=False)
    loc_m2 = hdr_m2.get("location", "")
    check("D2 la main reste servie par défaut malgré 2 branches expérimentales",
          V_STABLE in loc_m2 and V_EXP not in loc_m2 and V_EXP2 not in loc_m2,
          f"→ {loc_m2.rsplit('/', 1)[-1]}")

    _, page_a, _ = http(f"/catalog/{SLUG}?exp={TAG_EXP}")
    check("D3 la page d'une branche ne révèle pas l'autre (étanchéité des tags)",
          V_EXP.encode() in page_a and V_EXP2.encode() not in page_a)

    # La version publiée n'a pas été dépréciée par les publications expérimentales.
    statuses = psql(f"""SELECT version || '=' || status FROM plugin_versions
                        WHERE plugin_id = {ids['plugin']} ORDER BY version""").replace("\n", " ")
    check("D4 publier des expérimentales ne déprécie PAS la version publiée",
          f"{V_STABLE}=published" in statuses, statuses)

    # Le device retiré de la cohorte doit retomber sur le rollout général.
    psql(f"""DELETE FROM cohort_members
             WHERE identifier_value IN ('{EMAIL_IN}', '{UUID_IN}')""")
    d_left = directive(config_for(EMAIL_IN, UUID_IN, V_STABLE))
    check("D5 device sorti des cohortes : retour au rollout général, pas de blocage",
          d_left is None or d_left.get("target_version") == V_NEXT,
          f"target={d_left and d_left.get('target_version')} (rollout général = {V_NEXT})")

    if not KEEP:
        cleanup()
        print(f"\n  {Y}données E2E supprimées{N}")

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    total = len(RESULTS)
    print(f"\n{B}=== {passed}/{total} vérifications passées ==={N}")
    for name, ok_, detail in RESULTS:
        if not ok_:
            print(f"  {R}ÉCHEC{N} {name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\n{R}ERREUR: {exc}{N}")
        try:
            cleanup()
            print("données E2E supprimées malgré l'erreur")
        except Exception:
            pass
        sys.exit(2)
