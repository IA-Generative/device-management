#!/usr/bin/env python3
"""Mesure de concordance issue #5 — vue CLIENT, sans aucun secret.

Pour chaque plugin du catalogue : demande à /config/config.json la directive de
mise à jour (le checksum que le plugin attend), télécharge le binaire annoncé,
et compare. C'est exactement le contrôle que fait le plugin avant d'installer :
une divergence ici EST le « checksum mismatch » de l'issue #5.

Aucune écriture : pas de X-Client-UUID, donc aucune ligne campaign_device_status
(cf. main.py — l'upsert est conditionné à client_uuid non vide).
"""
import hashlib
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://bootstrap.fake-domain.name"


def get(url, headers=None, raw=False):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return (data, r.status, r.geturl()) if raw else (json.loads(data), r.status, r.geturl())


def main():
    plugins, _, _ = get(f"{BASE}/catalog/api/plugins")
    if isinstance(plugins, dict):
        plugins = plugins.get("plugins", [])
    print(f"Cible : {BASE}")
    print(f"{len(plugins)} plugins au catalogue\n")

    verdicts = []
    for p in plugins:
        slug = p["slug"]
        print(f"── {slug}  (dernière version annoncée : {p.get('latest_version')})")
        try:
            cfg, _, _ = get(
                f"{BASE}/config/config.json?device={slug}",
                {"X-Plugin-Version": "0.0.0-sonde-issue5"},
            )
        except Exception as exc:
            print(f"   /config → {type(exc).__name__}: {exc}\n")
            verdicts.append((slug, "CONFIG_KO", str(exc)))
            continue

        directive = cfg.get("update") or cfg.get("updateDirective") or cfg.get("update_directive")
        if not directive:
            # Pas de campagne active → aucun checksum promis. On mesure quand même
            # le téléchargement : un 404 apparu entre deux passages signale un
            # cache divergent que le pod refuse désormais de servir.
            try:
                blob, status, final = get(f"{BASE}/catalog/{slug}/download", raw=True)
                print(f"   pas de campagne active — téléchargement direct : HTTP {status}, "
                      f"{len(blob)} o → sha256:{hashlib.sha256(blob).hexdigest()}")
                print(f"   URL finale     : {final}\n")
                verdicts.append((slug, "SANS_CAMPAGNE", f"{len(blob)}o"))
            except Exception as exc:
                print(f"   pas de campagne active — téléchargement → {type(exc).__name__}: {exc}\n")
                verdicts.append((slug, "DOWNLOAD_KO", str(exc)))
            continue

        attendu = directive.get("checksum") or ""
        url = directive.get("artifact_url") or ""
        if url.startswith("/"):
            url = BASE + url
        print(f"   version cible  : {directive.get('target_version')}")
        print(f"   checksum promis: {attendu}")
        print(f"   binaire        : {url}")

        try:
            blob, status, final = get(url, raw=True)
        except Exception as exc:
            print(f"   téléchargement → {type(exc).__name__}: {exc}\n")
            verdicts.append((slug, "DOWNLOAD_KO", str(exc)))
            continue

        obtenu = "sha256:" + hashlib.sha256(blob).hexdigest()
        print(f"   octets servis  : {len(blob)} o → {obtenu}")
        print(f"   URL finale     : {final}")
        if not attendu:
            verdict = "SANS_CHECKSUM"
        elif obtenu == attendu.strip().lower():
            verdict = "CONCORDANT"
        else:
            verdict = "MISMATCH"
        print(f"   VERDICT        : {verdict}\n")
        verdicts.append((slug, verdict, f"attendu={attendu} obtenu={obtenu}"))

    print("═══ SYNTHÈSE ═══")
    for slug, v, _detail in verdicts:
        print(f"  {v:15s} {slug}")
    ko = [v for _, v, _ in verdicts if v == "MISMATCH"]
    print(f"\n{len(ko)} divergence(s) de checksum sur {len(verdicts)} plugin(s).")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
