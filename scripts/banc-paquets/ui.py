#!/usr/bin/env python3
"""Banc : les écrans, joués dans un vrai navigateur après le scénario run.py."""
import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
SHOTS = os.path.join(OUT, "ecrans")
os.makedirs(SHOTS, exist_ok=True)
ADMIN, API = "http://localhost:18091", "http://localhost:18090"
RES = []
def check(name, cond, detail=""):
    RES.append((name, bool(cond)))
    print(("  OK  " if cond else "  KO  ") + name + (f"  — {detail}" if detail and not cond else ""))
    return bool(cond)
def psql(sql):
    r = subprocess.run(["docker", "compose", "exec", "-T", "pg", "psql", "-U", "postgres", "-d", "bootstrap", "-t", "-A", "-c", sql], capture_output=True, text=True, cwd=HERE)
    if r.returncode:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()
PID = psql("SELECT id FROM plugins WHERE slug='banc-tb'")
def f(n): return os.path.join(OUT, n)

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    dialogs = []
    def on_dialog(d):
        dialogs.append(d.message)
        d.accept()
    page.on("dialog", on_dialog)

    print("\n== U1. Fiche du plugin : nature, origine, signature")
    page.goto(f"{ADMIN}/admin/catalog/{PID}?tab=versions")
    page.wait_for_load_state("networkidle")
    body = page.inner_text("#tab-versions")
    check("menu « Campagnes » présent", page.locator("nav a[href='/admin/campaigns'], a[href='/admin/campaigns']").count() > 0)
    check("colonnes Nature / Origine / Qui la reçoit", all(t in body for t in ("Nature", "Origine et signature", "Qui la recoit")), body[:200])
    check("natures affichées : Générale, Candidate, Essai", all(t in body for t in ("Générale", "Candidate", "Essai")))
    check("origine vérifiée et branche affichées", "Origine vérifiée" in body and "exp/recherche-v2" in body and "develop" in body)
    check("signature du DM affichée", "Signee par ce DM" in body)
    check("import sur confirmation signalé", "(sur confirmation)" in body)
    page.screenshot(path=f"{SHOTS}/1-fiche-versions.png", full_page=True)

    print("\n== U2. Envoi : aperçu, refus sans signature, origine vérifiée")
    page.click("text=+ Nouvelle version")
    page.set_input_files("#nv-file-input", f("banc-tb-0.26.0-rc.301.xpi"))
    page.wait_for_selector("#nv-import-preview div", timeout=15000)
    prev = page.inner_text("#nv-import-preview")
    check("sans signature : refus affiché", "Aucune signature" in prev or "signature d'origine" in prev, prev[:300])
    check("sans signature : « Suivant » désactivé", page.is_disabled("#nv-btn-step2"))
    page.screenshot(path=f"{SHOTS}/2-envoi-sans-signature.png", full_page=True)
    page.set_input_files("#nv-signature-input", f("banc-tb-0.26.0-rc.301.xpi.sig.json"))
    page.wait_for_selector("#nv-import-preview:has-text('Origine verifiee')", timeout=15000)
    prev = page.inner_text("#nv-import-preview")
    check("avec signature : nature Candidate et origine develop", "Candidate" in prev and "develop" in prev, prev[:300])
    check("configuration : remplacement annoncé, case cochée", "keycloakClientId" in prev and page.is_checked("#nv-adapt-checkbox"), prev[:400])
    check("avec signature : « Suivant » actif", not page.is_disabled("#nv-btn-step2"))
    page.screenshot(path=f"{SHOTS}/3-envoi-apercu.png", full_page=True)

    print("\n== U3. Envoi d'un essai depuis une branche hors liste, sur une cohorte")
    page.set_input_files("#nv-file-input", f("banc-tb-0.26.0-dev.300.xpi"))
    page.set_input_files("#nv-signature-input", f("banc-tb-0.26.0-dev.300.xpi.sig.json"))
    page.wait_for_selector("#nv-import-preview:has-text('exp/ecrans')", timeout=15000)
    prev = page.inner_text("#nv-import-preview")
    check("branche hors liste : annoncée, confirmable", "hors des références" in prev and not page.is_disabled("#nv-btn-step2"), prev[:300])
    page.click("#nv-btn-step2")
    check("version pré-remplie", page.input_value("#nv-version-input") == "0.26.0-dev.300", page.input_value("#nv-version-input"))
    page.fill("input[name='tag']", "exp-ecrans")
    page.fill("textarea[name='hypotheses']", "Les ecrans sont-ils clairs ?\nLe lien est-il facile a envoyer ?")
    page.click("text=Suivant — Deployer")
    page.check("input[name='campaign_kind'][value='experiment']")
    page.check("input[name='target_mode'][value='emails']")
    page.fill("textarea[name='emails']", "testeur-ui@banc.test")
    page.wait_for_timeout(1200)
    page.screenshot(path=f"{SHOTS}/4-envoi-deployer.png", full_page=True)
    page.click("#nv-btn-deploy")
    for _ in range(40):   # l'envoi passe par un refus 409 (confirmation) puis un second essai
        if psql("SELECT count(*) FROM plugin_versions WHERE version='0.26.0-dev.300'") == "1":
            break
        page.wait_for_timeout(1000)
    else:
        print("     erreur affichée :", page.inner_text("#nv-deploy-error")[:400])
        page.screenshot(path=f"{SHOTS}/4b-envoi-bloque.png", full_page=True)
    page.wait_for_load_state("networkidle")
    check("confirmation demandée pour la branche hors liste", any("hors des références" in d for d in dialogs), str(dialogs)[:300])
    check("essai importé", psql("SELECT status FROM plugin_versions WHERE version='0.26.0-dev.300'") == "experimental", psql("SELECT version,status FROM plugin_versions"))
    check("campagne d'essai active", psql("SELECT count(*) FROM campaigns c JOIN plugin_versions pv ON pv.id=c.version_id WHERE pv.version='0.26.0-dev.300' AND c.status='active' AND c.is_experiment") == "1")

    print("\n== U4. Inviter")
    page.goto(f"{ADMIN}/admin/catalog/{PID}?tab=versions")
    page.wait_for_load_state("networkidle")
    vid = psql("SELECT id FROM plugin_versions WHERE version='0.26.0-dev.300'")
    row_text = page.inner_text("#tab-versions")
    check("« Qui la reçoit » : essai sur sa cohorte", "Essai" in row_text and "cohorte «" in row_text, row_text[:300])
    page.click(f"[aria-controls='invite-{vid}']")
    check("panneau d'invitation ouvert", page.is_visible(f"#invite-{vid}"))
    link = page.input_value(f"#invite-link-{vid}")
    check("lien d'essai à copier", link == f"{API}/catalog/banc-tb?exp=exp-ecrans", link)
    check("message prêt, avec les questions", "Les ecrans sont-ils clairs" in page.input_value(f"#invite-msg-{vid}"))
    page.screenshot(path=f"{SHOTS}/5-inviter.png", full_page=True)
    before = int(psql("SELECT count(*) FROM cohort_members"))
    page.fill(f"#invite-emails-{vid}", "nouvelle.personne@banc.test, autre@banc.test")
    page.click(f"#invite-{vid} button:has-text('Ajouter a la cohorte')")
    page.wait_for_selector(f"#invite-{vid} .dm-flash--success", timeout=20000)
    check("deux personnes ajoutées à la cohorte", int(psql("SELECT count(*) FROM cohort_members")) == before + 2)
    check("retour : confirmation affichée", "Personnes ajoutees" in page.inner_text(f"#invite-{vid}"))

    print("\n== U5. Catalogue public")
    pub = ctx.new_page()
    pub.goto(f"{API}/catalog/banc-tb")
    t = pub.inner_text("body")
    check("sans lien : ni candidate ni essai", "rc.241" not in t and "dev." not in t and "Preversion" not in t)
    check("sans lien : phrase d'invitation", "sur invitation" in t)
    pub.screenshot(path=f"{SHOTS}/6-catalogue-general.png", full_page=True)
    page.click("button:has-text(\"L'annoncer a tous\")")
    page.wait_for_selector("button:has-text('Ne plus')", timeout=20000)
    pub.goto(f"{API}/catalog/banc-tb")
    t = pub.inner_text("body")
    check("candidate annoncée : bloc « Preversion »", "Preversion 0.25.0-rc.241" in t, t[:400])
    check("essais toujours cachés", "dev." not in t)
    pub.screenshot(path=f"{SHOTS}/7-catalogue-preversion.png", full_page=True)
    pub.click("text=Voir la preversion")
    pub.wait_for_load_state("networkidle")
    t = pub.inner_text("body")
    check("lien de la préversion : bandeau « Version d'essai »", "Version d'essai" in t and "0.25.0-rc.241" in t, t[:300])
    pub.goto(f"{API}/catalog/banc-tb?exp=exp-ecrans")
    t = pub.inner_text("body")
    check("lien d'essai : bandeau, questions, retour à la générale", "Version d'essai" in t and "Les ecrans sont-ils clairs" in t and "Revenir a la version generale" in t, t[:300])
    pub.screenshot(path=f"{SHOTS}/8-catalogue-essai.png", full_page=True)

    check("aucune erreur de script dans les pages", not errors, str(errors)[:400])
    browser.close()

print(f"\n{sum(1 for _, ok in RES if ok)}/{len(RES)} contrôles d'écran passés")
sys.exit(0 if all(ok for _, ok in RES) else 1)
