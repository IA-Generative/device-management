"""Export parc → bus de la bêta (app/services/parc_export + parc_mapping).

Verrouille le contrat de sortie :
- mapping span → fonction (allowlist figée, span inconnu jamais exporté) ;
- empreinte canonique (vecteur figé, identique côté bus) ;
- deltas en VALEURS ABSOLUES, battement vide toujours envoyé, rejeu sans effet
  (clause de rejeu : même seq réémis après réponse perdue → 200 sans effet) ;
- 409 → instantané complet aussitôt ; 401/400 → journalisé, PAS de boucle ;
- ANTI-FUITE : aucun uuid/email/IP/hostname des fixtures dans le JSON exporté ;
- préalables d'audit : persistance locale découplée du forward OTLP,
  `received_at` → `created_at`, insertion download_events avant le 302.

La base est émulée en mémoire (FauxBase, patron des tests du dépôt) ; le bus
est un VRAI serveur HTTP local minimal (FauxBus) reproduisant 200/409/401
selon le contrat.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import socket
import sys
import threading
import types
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

from app.services import parc_export as pe
from app.services import parc_mapping as pm

SECRET = "s3cret-test"

AUJOURDHUI = datetime.now(UTC).date()
HIER = AUJOURDHUI - timedelta(days=1)

UUID_A = "3f2c9b1e-8a4d-4f6b-9c2d-1e5a7b3c9d0f"
UUID_B = "a1b2c3d4-e5f6-4a0b-8c1d-2e3f4a5b6c7d"


# ═══════════════════════════════════════════════════════════════════════════
# FauxBase — émulation mémoire du sous-ensemble SQL utilisé par parc_export
# ═══════════════════════════════════════════════════════════════════════════

class FauxBase:
    def __init__(self):
        self.evenements = []          # (id, client_uuid, span_name, ts, attributes, version)
        self.agregat = {}             # (jour,plugin,version,fonction) -> [appels, postes, erreurs, seq]
        self.version_etat = {}        # (jour,plugin,version) -> [canal, publiee_le, inst, rec, tel, seq]
        self.etat = {"curseur_dte": 0, "seq": 0, "dernier_envoi": None,
                     "dernier_code": None, "resyncs": 0, "dernier_jour": None}
        self.journal = []             # (heure, type, seq, lignes, code, duree_ms)
        self.plugins = {}             # slug -> id (catalogue du site)
        self.aliases = {}             # alias -> slug (plugin_aliases)
        self.catalogue_versions = {}  # slug -> [(version, status, published_date, maturity)]
        self.installations = {}       # slug -> [(version, n30, n7)]
        self.telechargements = {}     # slug -> [(version, count)]


def _slugs_des_ids(db: "FauxBase", ids) -> list[str]:
    """Traduit une liste d'ids de plugins en slugs (les fixtures sont par slug)."""
    voulus = set(ids or [])
    return [sl for sl, i in db.plugins.items() if i in voulus]


class FauxCurseur:
    def __init__(self, db: FauxBase):
        self.db = db
        self._rows = []

    # -- protocole psycopg2 minimal
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def execute(self, sql, params=None):  # noqa: C901 - aiguillage volontairement plat
        db, p = self.db, (params or ())
        s = " ".join(sql.split())
        self._rows = []

        if s.startswith("SELECT pg_try_advisory_lock") or "pg_advisory_unlock" in s:
            self._rows = [(True,)]
        elif s.startswith("INSERT INTO parc_export_etat"):
            pass
        elif s.startswith("SELECT curseur_dte"):
            e = db.etat
            self._rows = [(e["curseur_dte"], e["seq"], e["dernier_envoi"],
                           e["dernier_code"], e["resyncs"], e["dernier_jour"])]
        elif "FROM device_telemetry_events WHERE id >" in s:
            apres, limite = int(p[0]), int(p[1])
            self._rows = [ev for ev in db.evenements if ev[0] > apres][:limite]
        elif s.startswith("INSERT INTO parc_agregat"):
            jour, plugin, version, fonction, appels, erreurs, seq = p
            cle = (jour, plugin, version, fonction)
            ligne = db.agregat.setdefault(cle, [0, 0, 0, 0])
            ligne[0] += appels
            ligne[2] += erreurs
            ligne[3] = seq
        elif s.startswith("UPDATE parc_agregat SET postes"):
            jour, version, cles, sentinelles, jour2, plugin, version2, fonction = p
            postes = {
                ev[1] for ev in db.evenements
                if ev[3] is not None and ev[3].astimezone(UTC).date() == jour
                and (ev[5] or "") == version
                and ((ev[4] or {}).get("plugin.action") or ev[2]) in cles
                and ev[1] and ev[1] not in sentinelles
            }
            cle = (jour2, plugin, version2, fonction)
            if cle in db.agregat:
                db.agregat[cle][1] = len(postes)
        elif s.startswith("UPDATE parc_export_etat SET curseur_dte"):
            db.etat["curseur_dte"] = int(p[0])
        elif s.startswith("SELECT p.id, p.slug FROM plugins p"):
            # Résolution par slug OU alias, sur la LISTE de candidats.
            cibles = set(p[0] or ())
            slugs = [sl for sl in db.plugins if sl in cibles]
            slugs += [sl for al, sl in db.aliases.items()
                      if al in cibles and sl in db.plugins and sl not in slugs]
            self._rows = [(db.plugins[sl], sl) for sl in slugs]
        elif s.startswith("SELECT pv.version"):
            self._rows = [l for sl in _slugs_des_ids(db, p[0])
                          for l in db.catalogue_versions.get(sl, [])]
        elif s.startswith("SELECT COALESCE(pi.installed_version"):
            self._rows = [l for sl in _slugs_des_ids(db, p[0])
                          for l in db.installations.get(sl, [])]
        elif s.startswith("SELECT version_tag"):
            self._rows = [l for sl in list(p[0])
                          for l in db.telechargements.get(sl, [])]
        elif s.startswith("SELECT plugin, version, canal"):
            jour = p[0]
            self._rows = [
                (k[1], k[2], v[0], v[1], v[2], v[3], v[4])
                for k, v in db.version_etat.items() if k[0] == jour
            ]
        elif s.startswith("INSERT INTO parc_version_etat"):
            jour, plugin, version, canal, publiee_le, inst, rec, tel, seq = p
            db.version_etat[(jour, plugin, version)] = [canal, publiee_le, inst, rec, tel, seq]
        elif s.startswith("DELETE FROM parc_version_etat WHERE jour = %s AND plugin"):
            db.version_etat.pop((p[0], p[1], p[2]), None)
        elif s.startswith("SELECT jour, plugin, version, canal"):
            fenetre = set(p[0])
            seq_min = int(p[1]) if len(p) > 1 else None
            self._rows = [
                (k[0], k[1], k[2], v[0], v[1], v[2], v[3], v[4])
                for k, v in db.version_etat.items()
                if k[0] in fenetre and (seq_min is None or v[5] > seq_min)
            ]
        elif s.startswith("SELECT jour, plugin, version, fonction"):
            fenetre = set(p[0])
            seq_min = int(p[1]) if len(p) > 1 else None
            self._rows = [
                (k[0], k[1], k[2], k[3], v[0], v[1], v[2])
                for k, v in db.agregat.items()
                if k[0] in fenetre and (seq_min is None or v[3] > seq_min)
            ]
        elif s.startswith("DELETE FROM parc_agregat WHERE jour <"):
            db.agregat = {k: v for k, v in db.agregat.items() if k[0] >= p[0]}
        elif s.startswith("DELETE FROM parc_version_etat WHERE jour <"):
            db.version_etat = {k: v for k, v in db.version_etat.items() if k[0] >= p[0]}
        elif s.startswith("INSERT INTO parc_export_journal"):
            db.journal.append((datetime.now(UTC),) + tuple(p))
        elif s.startswith("DELETE FROM parc_export_journal"):
            db.journal = db.journal[-int(p[0]):]
        elif s.startswith("UPDATE parc_export_etat SET dernier_envoi"):
            db.etat["dernier_envoi"] = datetime.now(UTC)
            db.etat["dernier_code"] = p[0]
            db.etat["resyncs"] += int(p[1])
        elif s.startswith("UPDATE parc_export_etat SET seq"):
            db.etat["seq"] = int(p[0])
        elif s.startswith("UPDATE parc_export_etat SET dernier_jour"):
            db.etat["dernier_jour"] = p[0]
        elif s.startswith("SELECT heure, type"):
            self._rows = list(reversed(db.journal))[:10]
        elif s.startswith("SELECT COUNT(*) FROM parc_export_journal"):
            self._rows = [(sum(1 for j in db.journal if j[4] != 200),)]
        else:  # pragma: no cover - garde-fou : une requête inconnue doit se voir
            raise AssertionError(f"FauxBase: requête non émulée: {s[:120]}")


class FauxConnexion:
    def __init__(self, db: FauxBase):
        self.db = db
        self.autocommit = False

    def cursor(self):
        return FauxCurseur(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ═══════════════════════════════════════════════════════════════════════════
# FauxBus — serveur HTTP local minimal reproduisant le contrat (200/409/401)
# ═══════════════════════════════════════════════════════════════════════════

class _FauxBusHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silencieux
        pass

    def do_POST(self):
        srv = self.server
        corps = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        srv.requetes.append((self.path, corps))
        if self.headers.get("X-Parc-Secret") != srv.secret_attendu:
            return self._repondre(401, {"detail": "mauvais secret"})
        donnees = json.loads(corps)
        if srv.mode == "mourir_une_fois":
            # Applique puis coupe la connexion SANS répondre (réponse perdue).
            srv.mode = "normal"
            if self.path.endswith("/delta"):
                self._appliquer_delta(donnees, appliquer_seulement=True)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if self.path.endswith("/_parc/instantane"):
            srv.etat["usage"] = {}
            for pl in donnees.get("plugins", []):
                for u in pl.get("usage", []):
                    srv.etat["usage"][(u["jour"], u["plugin"], u["version"], u["fonction"])] = (
                        u["appels"], u["postes"], u["erreurs"])
            srv.etat["dernier_seq"] = donnees["seq"]
            srv.instantanes += 1
            return self._repondre(200, {"seq": donnees["seq"]})
        if self.path.endswith("/_parc/delta"):
            return self._appliquer_delta(donnees)
        return self._repondre(404, {})

    def _appliquer_delta(self, d, appliquer_seulement=False):
        srv = self.server
        dernier = srv.etat["dernier_seq"]
        if d["seq"] == dernier and d["seq_precedente"] == d["seq"] - 1:
            # CLAUSE DE REJEU : delta déjà acquitté → 200 SANS effet.
            if not appliquer_seulement:
                return self._repondre(200, {"seq": d["seq"]})
            return None
        if d["seq"] == dernier + 1 and d["seq_precedente"] == dernier:
            for u in d.get("usage", []):
                srv.etat["usage"][(u["jour"], u["plugin"], u["version"], u["fonction"])] = (
                    u["appels"], u["postes"], u["erreurs"])
            srv.etat["dernier_seq"] = d["seq"]
            srv.applications += 1
            if not appliquer_seulement:
                return self._repondre(200, {"seq": d["seq"]})
            return None
        # Chaînon manquant / divergence → resynchronisation demandée.
        if not appliquer_seulement:
            return self._repondre(409, {"resynchroniser": True})
        return None

    def _repondre(self, code, corps):
        octets = json.dumps(corps).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(octets)))
        self.end_headers()
        self.wfile.write(octets)


@pytest.fixture()
def faux_bus():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FauxBusHandler)
    srv.secret_attendu = SECRET
    srv.mode = "normal"
    srv.etat = {"dernier_seq": 0, "usage": {}}
    srv.requetes = []
    srv.instantanes = 0
    srv.applications = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures communes
# ═══════════════════════════════════════════════════════════════════════════

def _evenement(evt_id, uuid, span, jour, version="0.2.1", attributes=None, heure=10):
    ts = datetime(jour.year, jour.month, jour.day, heure, 0, 0, tzinfo=UTC)
    return (evt_id, uuid, span, ts, attributes or {}, version)


def _base_avec_fixtures() -> FauxBase:
    """Événements VOLONTAIREMENT pleins de données personnelles : uuids, email,
    IP, hostname — rien de tout cela ne doit sortir (test anti-fuite)."""
    attrs_pii = {
        "user.email": "agent.teste@interieur.gouv.fr",
        "host.name": "poste-interne.local",
        "client.ip": "10.1.2.3",
    }
    db = FauxBase()
    db.evenements = [
        _evenement(1, UUID_A, "SummarizeSelection", AUJOURDHUI, attributes=dict(attrs_pii)),
        _evenement(2, UUID_B, "SummarizeSelection", AUJOURDHUI, attributes=dict(attrs_pii)),
        _evenement(3, UUID_A, "AssistantRun", AUJOURDHUI,
                   attributes={**attrs_pii, "assistant.ok": False}),
        # Sentinelles : comptent des appels, jamais des postes.
        _evenement(4, "telemetry-open", "SummarizeSelection", AUJOURDHUI, attributes=dict(attrs_pii)),
        # Span hors allowlist : jamais exporté.
        _evenement(5, UUID_A, "OpenSettings", AUJOURDHUI),
        # Veille : dans la fenêtre.
        _evenement(6, UUID_B, "TranslateSelection", HIER, attributes=dict(attrs_pii)),
        # Hors fenêtre : ignoré (le curseur avance quand même).
        _evenement(7, UUID_A, "SummarizeSelection", AUJOURDHUI - timedelta(days=5)),
    ]
    db.plugins = {"mirai-libreoffice": 1, "mirai-matisse": 2}
    db.aliases = {}
    db.catalogue_versions = {
        "mirai-libreoffice": [("0.2.1", "published", date(2026, 8, 15), "release")],
        "mirai-matisse": [],
    }
    db.installations = {"mirai-libreoffice": [("0.2.1", 12, 5)]}
    db.telechargements = {"mirai-libreoffice": [("0.2.1", 40)]}
    return db


@pytest.fixture()
def env_export(monkeypatch, faux_bus):
    """Branche parc_export sur FauxBase + FauxBus, export actif."""
    from app.settings import settings
    db = _base_avec_fixtures()
    monkeypatch.setattr(pe, "get_db_connection", lambda: FauxConnexion(db))
    monkeypatch.setattr(settings, "parc_export_url", faux_bus.base_url)
    monkeypatch.setattr(settings, "parc_export_secret", SECRET)
    monkeypatch.setattr(settings, "parc_export_enabled", True)
    monkeypatch.setattr(settings, "parc_export_intervalle_s", 300)
    monkeypatch.setattr(settings, "parc_instance_label", "dm-test")
    return db


@pytest.fixture(autouse=True)
def _reset_spans_inconnus():
    pm._spans_inconnus_vus.clear()
    yield
    pm._spans_inconnus_vus.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Mapping span → fonction
# ═══════════════════════════════════════════════════════════════════════════

def test_mapping_span_connu():
    assert pm.mapper_evenement("SummarizeSelection", {}) == ("libreoffice", "texte.resume", False)
    assert pm.mapper_evenement("CorrectSelection", None) == ("libreoffice", "texte.corrige", False)


def test_mapping_plugin_action_prioritaire_sur_span_name():
    """La clé est l'attribut plugin.action s'il est présent, sinon span_name."""
    assert pm.mapper_evenement("SpanQuelconque", {"plugin.action": "AssistantOpen"}) == (
        "libreoffice", "assistant.ouvert", False)


def test_mapping_erreur_assistant_run():
    """AssistantRun ok=false → une erreur de conversation.envoyee ; ok=true → zéro."""
    assert pm.mapper_evenement("AssistantRun", {"assistant.ok": False}) == (
        "libreoffice", "conversation.envoyee", True)
    assert pm.mapper_evenement("AssistantRun", {"assistant.ok": True}) == (
        "libreoffice", "conversation.envoyee", False)
    assert pm.mapper_evenement("AssistantRun", {}) == (
        "libreoffice", "conversation.envoyee", False)


def test_mapping_span_inconnu_jamais_exporte_et_journalise_une_fois(caplog):
    """Plancher assumé : AssistantToolCall/LlmRelayError non attribuables → non
    exportés ; un span inconnu est journalisé UNE fois par nom, en debug."""
    with caplog.at_level("DEBUG", logger="dm-parc-mapping"):
        assert pm.mapper_evenement("AssistantToolCall", {"tool.ok": False}) is None
        assert pm.mapper_evenement("LlmRelayError", {}) is None
        assert pm.mapper_evenement("AssistantToolCall", {"tool.ok": False}) is None
    messages = [r.getMessage() for r in caplog.records if "non mappé" in r.getMessage()]
    assert sum("AssistantToolCall" in m for m in messages) == 1
    assert sum("LlmRelayError" in m for m in messages) == 1


def test_catalogue_ferme_respecte():
    """Chaque fonction mappée appartient au catalogue FERMÉ de son plugin."""
    for plugin, table in pm.SPAN_VERS_FONCTION.items():
        for fonction in table.values():
            assert fonction in pm.FONCTIONS[plugin], (plugin, fonction)


# ═══════════════════════════════════════════════════════════════════════════
# Empreinte canonique — vecteur figé (identique côté bus)
# ═══════════════════════════════════════════════════════════════════════════

_VECTEUR_VERSIONS = [
    {"plugin": "libreoffice", "version": "0.2.1", "jour": "2026-09-01", "canal": "release",
     "publiee_le": "2026-08-15", "installations": 12, "installations_recentes": 5,
     "telechargements_cumules": 40},
    {"plugin": "matisse", "version": "1.0.0", "jour": "2026-09-01", "canal": "beta",
     "publiee_le": "", "installations": 3, "installations_recentes": 1,
     "telechargements_cumules": 7},
]
_VECTEUR_USAGE = [
    {"jour": "2026-09-01", "plugin": "libreoffice", "version": "0.2.1",
     "fonction": "texte.resume", "appels": 10, "postes": 4, "erreurs": 1},
    {"jour": "2026-08-31", "plugin": "libreoffice", "version": "0.2.1",
     "fonction": "conversation.envoyee", "appels": 2, "postes": 2, "erreurs": 0},
]


def test_empreinte_vecteur_fige():
    """Le moindre changement de format casse ce test — c'est voulu : l'empreinte
    doit rester identique à celle calculée côté bus."""
    assert pe.lignes_canoniques(_VECTEUR_VERSIONS, _VECTEUR_USAGE) == [
        "u|libreoffice|2026-08-31|0.2.1|conversation.envoyee|2|2|0",
        "u|libreoffice|2026-09-01|0.2.1|texte.resume|10|4|1",
        "v|libreoffice|0.2.1|2026-09-01|release|2026-08-15|12|5|40",
        "v|matisse|1.0.0|2026-09-01|beta||3|1|7",
    ]
    assert pe.calculer_empreinte(_VECTEUR_VERSIONS, _VECTEUR_USAGE) == (
        "ba3ba559c59039c4c5d768aabf9e4696618f2d712d6d9512e2153d6e87a91395")


def test_empreinte_vide():
    import hashlib
    assert pe.calculer_empreinte([], []) == hashlib.sha256(b"").hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# Bornes du contrat
# ═══════════════════════════════════════════════════════════════════════════

def test_bornes_compteurs_et_erreurs_max_appels():
    assert pe.borner(-3) == 0
    assert pe.borner(10**8) == 10**7
    ligne = pe._ligne_usage("2026-09-01", "libreoffice", "1.0", "texte.resume",
                            appels=2, postes=1, erreurs=5)
    assert ligne["erreurs"] == 2  # erreurs ≤ appels, toujours


def test_bornes_corps_et_lignes_usage(monkeypatch):
    assert pe.serialiser({"x": "y" * (pe.MAX_CORPS_OCTETS + 10)}) is None
    monkeypatch.setattr(pe, "MAX_LIGNES_USAGE", 3)
    usage = [dict(_VECTEUR_USAGE[0], fonction=f"f{i}") for i in range(6)]
    delta = pe.construire_delta(seq=2, seq_precedente=1, empreinte="e",
                                versions=[], usage=usage, jours_clos=[])
    assert len(delta["usage"]) == 3


# ═══════════════════════════════════════════════════════════════════════════
# Cycle complet contre le faux bus (FauxBase + FauxBus)
# ═══════════════════════════════════════════════════════════════════════════

def test_premier_demarrage_instantane_puis_battement(env_export, faux_bus):
    resultat = pe.executer_cycle(force=True)
    assert resultat["statut"] == "envoye"
    # Premier démarrage : un instantané (seq 1) puis le delta battement (seq 2).
    chemins = [c for c, _ in faux_bus.requetes]
    assert chemins == ["/_parc/instantane", "/_parc/delta"]
    assert env_export.etat["seq"] == 2
    assert env_export.etat["dernier_code"] == 200
    assert env_export.etat["dernier_jour"] == AUJOURDHUI

    # L'instantané porte l'état ABSOLU : appels par combinaison, postes sans
    # les sentinelles (3 appels texte.resume dont 1 sentinelle → 2 postes).
    etat_bus = faux_bus.etat["usage"]
    cle_resume = (AUJOURDHUI.isoformat(), "libreoffice", "0.2.1", "texte.resume")
    assert etat_bus[cle_resume] == (3, 2, 0)
    cle_conv = (AUJOURDHUI.isoformat(), "libreoffice", "0.2.1", "conversation.envoyee")
    assert etat_bus[cle_conv] == (1, 1, 1)   # AssistantRun ok=false → 1 erreur
    cle_trad = (HIER.isoformat(), "libreoffice", "0.2.1", "texte.traduit")
    assert etat_bus[cle_trad] == (1, 1, 0)
    # Le span hors allowlist et l'événement hors fenêtre n'existent nulle part.
    assert not any("OpenSettings" in str(k) for k in etat_bus)
    # Le battement qui suit l'instantané est VIDE (tout est déjà acquitté).
    delta = json.loads(faux_bus.requetes[1][1])
    assert delta["schema"] == "parc.delta.v1"
    assert delta["usage"] == [] and delta["versions"] == []
    # Le curseur a bien avancé jusqu'au dernier événement (jamais de re-scan).
    assert env_export.etat["curseur_dte"] == 7


def test_delta_porte_des_valeurs_absolues(env_export, faux_bus):
    pe.executer_cycle(force=True)
    # Deux nouveaux résumés du même poste : le delta suivant porte le TOTAL
    # du jour (5), pas l'incrément (2).
    env_export.evenements.append(_evenement(8, UUID_A, "SummarizeSelection", AUJOURDHUI))
    env_export.evenements.append(_evenement(9, UUID_A, "SummarizeSelection", AUJOURDHUI))
    resultat = pe.executer_cycle(force=True)
    assert resultat["statut"] == "envoye"
    delta = json.loads(faux_bus.requetes[-1][1])
    lignes = {(u["jour"], u["fonction"]): u for u in delta["usage"]}
    ligne = lignes[(AUJOURDHUI.isoformat(), "texte.resume")]
    assert ligne["appels"] == 5 and ligne["postes"] == 2
    # Seule la combinaison touchée repart — pas les autres (delta ≠ instantané).
    assert (HIER.isoformat(), "texte.traduit") not in lignes
    assert faux_bus.etat["usage"][(AUJOURDHUI.isoformat(), "libreoffice", "0.2.1",
                                   "texte.resume")] == (5, 2, 0)


def test_battement_vide_toujours_envoye(env_export, faux_bus):
    pe.executer_cycle(force=True)
    n_avant = len(faux_bus.requetes)
    resultat = pe.executer_cycle(force=True)
    assert resultat["statut"] == "envoye"
    assert len(faux_bus.requetes) == n_avant + 1
    delta = json.loads(faux_bus.requetes[-1][1])
    assert delta["usage"] == [] and delta["versions"] == [] and delta["jours_clos"] == []
    assert delta["seq"] == delta["seq_precedente"] + 1


def test_rejeu_sans_effet_apres_reponse_perdue(env_export, faux_bus):
    """Clause de rejeu : le bus a reçu et appliqué le delta mais la réponse est
    perdue → le DM réémet le MÊME seq au cycle suivant, le bus ré-acquitte 200
    SANS effet — pas d'erreur, pas de resynchronisation."""
    pe.executer_cycle(force=True)
    env_export.evenements.append(_evenement(8, UUID_B, "AssistantRun", AUJOURDHUI))
    faux_bus.mode = "mourir_une_fois"
    resultat = pe.executer_cycle(force=True)
    assert resultat["statut"] == "refuse" and resultat["code"] == 0
    seq_perdu = resultat["seq"]
    applications_avant = faux_bus.applications
    etat_bus_avant = dict(faux_bus.etat["usage"])

    resultat2 = pe.executer_cycle(force=True)
    assert resultat2["statut"] == "envoye"
    assert resultat2["seq"] == seq_perdu                      # même seq réémis
    assert faux_bus.applications == applications_avant        # 200 sans effet
    assert faux_bus.etat["usage"] == etat_bus_avant           # état bus inchangé
    assert faux_bus.instantanes == 1                          # aucune resync
    assert env_export.etat["seq"] == seq_perdu                # le DM est acquitté


def test_409_declenche_un_instantane_immediat(env_export, faux_bus):
    pe.executer_cycle(force=True)
    # Divergence simulée côté bus (état perdu / chaînon manquant).
    faux_bus.etat["dernier_seq"] = 99
    resultat = pe.executer_cycle(force=True)
    assert resultat["statut"] == "resynchronise" and resultat["code"] == 200
    chemins = [c for c, _ in faux_bus.requetes[-2:]]
    assert chemins == ["/_parc/delta", "/_parc/instantane"]
    assert env_export.etat["resyncs"] == 1
    # L'instantané prend le seq suivant et le DM est de nouveau acquitté.
    assert env_export.etat["seq"] == resultat["seq"]
    instantane = json.loads(faux_bus.requetes[-1][1])
    assert instantane["schema"] == "parc.instantane.v1"
    assert {p["plugin"] for p in instantane["plugins"]} == {"libreoffice", "matisse"}


def test_401_journalise_sans_boucler(env_export, faux_bus):
    faux_bus.secret_attendu = "autre-secret"
    n_avant = len(faux_bus.requetes)
    resultat = pe.executer_cycle(force=True)
    # Premier démarrage : l'instantané prend le 401 → cycle abandonné, UNE seule
    # requête (pas de boucle, pas de delta derrière).
    assert resultat == {"statut": "instantane_refuse", "code": 401}
    assert len(faux_bus.requetes) == n_avant + 1
    assert env_export.etat["dernier_code"] == 401
    assert env_export.etat["seq"] == 0
    assert env_export.journal[-1][4] == 401  # journalisé, avec le code


def test_cloture_de_journee_envoie_un_instantane_et_les_jours_clos(env_export, faux_bus):
    pe.executer_cycle(force=True)
    # On simule « premier cycle après minuit UTC » : le dernier envoi acquitté
    # date d'hier → instantané pour figer J-1, et le delta liste les jours
    # sortis de la fenêtre.
    env_export.etat["dernier_jour"] = HIER
    resultat = pe.executer_cycle(force=True)
    assert resultat["statut"] == "envoye"
    chemins = [c for c, _ in faux_bus.requetes[-2:]]
    assert chemins == ["/_parc/instantane", "/_parc/delta"]
    delta = json.loads(faux_bus.requetes[-1][1])
    assert delta["jours_clos"] == [(HIER - timedelta(days=1)).isoformat()]
    assert env_export.etat["dernier_jour"] == AUJOURDHUI


def test_desactive_ne_fait_rien(env_export, faux_bus, monkeypatch):
    from app.settings import settings
    monkeypatch.setattr(settings, "parc_export_enabled", False)
    assert pe.executer_cycle() == {"statut": "desactive"}
    assert faux_bus.requetes == []


def test_l_empreinte_du_delta_couvre_tout_l_etat_de_la_fenetre(env_export, faux_bus):
    pe.executer_cycle(force=True)
    env_export.evenements.append(_evenement(8, UUID_A, "CorrectSelection", AUJOURDHUI))
    pe.executer_cycle(force=True)
    delta = json.loads(faux_bus.requetes[-1][1])
    # L'empreinte se recalcule depuis l'état COMPLET (fenêtre), pas depuis le
    # delta seul — même formule que côté bus.
    versions, usage = pe._etat_fenetre(FauxConnexion(env_export), [AUJOURDHUI, HIER])
    assert delta["empreinte"] == pe.calculer_empreinte(versions, usage)


# ═══════════════════════════════════════════════════════════════════════════
# Anti-fuite (obligatoire) : rien de personnel dans le JSON exporté
# ═══════════════════════════════════════════════════════════════════════════

def test_anti_fuite_aucune_donnee_personnelle_dans_les_exports(env_export, faux_bus):
    pe.executer_cycle(force=True)
    env_export.evenements.append(
        _evenement(8, UUID_B, "AssistantRun", AUJOURDHUI,
                   attributes={"user.email": "x@y.fr", "client.ip": "192.168.1.27",
                               "host.name": "poste-interne.local"}))
    pe.executer_cycle(force=True)
    assert len(faux_bus.requetes) >= 3
    for _chemin, corps in faux_bus.requetes:
        texte = corps.decode("utf-8")
        assert UUID_A not in texte and UUID_B not in texte, "client_uuid exporté !"
        assert "telemetry-open" not in texte
        assert "@" not in texte, "email exporté !"
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", texte), "IP exportée !"
        assert "poste-interne" not in texte and ".local" not in texte, "hostname exporté !"
        assert "attributes" not in texte, "attributs bruts exportés !"


# ═══════════════════════════════════════════════════════════════════════════
# Préalables relevés par audit (app/main.py, app/admin/services/devices.py)
# ═══════════════════════════════════════════════════════════════════════════

def _charger_main():
    os.environ.setdefault("DATABASE_URL", "postgresql://dev:dev@localhost:5432/bootstrap")
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = MagicMock()
    fake_psycopg2.Error = Exception
    sys.modules["psycopg2"] = fake_psycopg2
    sys.modules.pop("app.main", None)
    mod = importlib.import_module("app.main")
    mod.psycopg2 = fake_psycopg2
    return mod


def test_forward_en_echec_persiste_quand_meme_localement(monkeypatch):
    """Découplage : la persistance locale a lieu AVANT le forward ; un amont
    OTLP en panne reste une erreur de job (retry), mais les spans sont en base."""
    mod = _charger_main()
    from app.postgres_queue import QueueJob
    appels = []
    monkeypatch.setattr(mod, "_persist_telemetry_spans",
                        lambda body, cu: appels.append(("persist", cu)))
    monkeypatch.setattr(mod, "_forward_telemetry_to_upstream",
                        lambda body, content_type, user_agent: appels.append(("forward",))
                        or types.SimpleNamespace(status_code=502))
    job = QueueJob(id="j1", topic="telemetry.forward",
                   payload={"body_b64": mod._b64url_encode(b"{}"),
                            "client_uuid": "uuid-x"},
                   attempts=1, max_attempts=8, dedupe_key=None)
    with pytest.raises(RuntimeError, match="status=502"):
        mod._process_queue_job(job)
    assert appels == [("persist", "uuid-x"), ("forward",)]


def test_forward_retry_ne_repersiste_pas(monkeypatch):
    """Idempotence du retry : au 2e passage (attempts>1), les spans sont déjà
    en base — on ne les réinsère pas, seul le forward est rejoué."""
    mod = _charger_main()
    from app.postgres_queue import QueueJob
    appels = []
    monkeypatch.setattr(mod, "_persist_telemetry_spans",
                        lambda body, cu: appels.append("persist"))
    monkeypatch.setattr(mod, "_forward_telemetry_to_upstream",
                        lambda body, content_type, user_agent: types.SimpleNamespace(status_code=200))
    job = QueueJob(id="j1", topic="telemetry.forward",
                   payload={"body_b64": mod._b64url_encode(b"{}"), "client_uuid": "u"},
                   attempts=2, max_attempts=8, dedupe_key=None)
    mod._process_queue_job(job)   # 200 : pas d'erreur
    assert appels == []


def test_activite_device_lit_created_at():
    """Bug received_at : la colonne s'appelle created_at — la requête ne doit
    plus référencer une colonne inexistante (UndefinedColumn en prod)."""
    from app.admin.services import devices

    class Cur:
        description = [("span_name",), ("span_ts",), ("attributes",),
                       ("plugin_version",), ("received_at",)]

        def execute(self, sql, params):
            self.sql = " ".join(sql.split())

        def fetchall(self):
            return []

    cur = Cur()
    devices.get_device_activity(cur, "uuid-x")
    assert "created_at AS received_at" in cur.sql
    assert "ORDER BY created_at DESC" in cur.sql
    # Plus aucune référence à received_at ailleurs que l'alias.
    assert cur.sql.count("received_at") == 1


def test_telechargement_catalogue_insere_download_events():
    """La route /catalog/{slug}/download insère une ligne download_events
    AVANT le 302 — et un échec d'insert ne casse pas le téléchargement."""
    from fastapi.testclient import TestClient
    mod = _charger_main()

    class Cur:
        def __init__(self, rate_insert=False):
            self.rate_insert = rate_insert
            self.inserts = []
            self._reponse = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT id, device_type"):
                self._reponse = (1, "libreoffice")
            elif s.startswith("SELECT pv.version"):
                self._reponse = ("9.9.9",)
            elif s.startswith("INSERT INTO download_events"):
                if self.rate_insert:
                    raise RuntimeError("insert KO")
                self.inserts.append(params)
                self._reponse = None
            else:
                self._reponse = None

        def fetchone(self):
            return self._reponse

    class Conn:
        def __init__(self, cur):
            self._cur = cur
            self.autocommit = False

        def cursor(self):
            return self._cur

        def close(self):
            pass

    for rate_insert in (False, True):
        cur = Cur(rate_insert=rate_insert)
        mod.psycopg2.connect = MagicMock(return_value=Conn(cur))
        mod._pooled_conn = lambda: None
        client = TestClient(mod.app)
        reponse = client.get("/catalog/mirai-libreoffice/download", follow_redirects=False)
        assert reponse.status_code == 302
        assert "mirai-libreoffice-9.9.9" in reponse.headers["location"]
        if rate_insert:
            assert cur.inserts == []       # échec avalé, téléchargement intact
        else:
            assert cur.inserts == [("mirai-libreoffice", "9.9.9")]


# ── Résolution du plugin : nom d'export ET slug, par slug OU alias ────────────
# Relevé du DGX le 2026-09-05 : le catalogue porte « matisse » comme SLUG, et
# « mirai-matisse » n'existe NULLE PART — ni slug, ni alias. Chercher le seul slug
# canonique rendait un ensemble vide, et _versions_courantes faisait un `continue`
# SILENCIEUX : 6 versions et 90 installations sur 120 jamais exportées.


def _base_dgx() -> FauxBase:
    """Le catalogue réel du DGX : matisse est un slug, libreoffice est un alias."""
    db = _base_avec_fixtures()
    db.plugins = {"mirai-libreoffice": 1, "matisse": 2}
    db.aliases = {"libreoffice": "mirai-libreoffice", "matisse": "matisse"}
    db.catalogue_versions = {
        "mirai-libreoffice": [("0.2.1", "published", date(2026, 8, 15), "release")],
        "matisse": [("0.13.1", "published", date(2026, 8, 20), "release")],
    }
    db.installations = {"mirai-libreoffice": [("0.2.1", 12, 5)],
                        "matisse": [("0.13.1", 90, 42)]}
    db.telechargements = {"mirai-libreoffice": [("0.2.1", 40)],
                          "matisse": [("0.13.1", 77)]}
    return db


def test_plugin_resolu_par_nom_export_est_exporte():
    """Sur le catalogue du DGX, les deux plugins sortent — par des chemins différents.

    « matisse » se résout comme slug direct, « libreoffice » par son alias vers
    mirai-libreoffice. Aucun des deux ne passe par le slug canonique du mapping.
    """
    db = _base_dgx()
    with FauxConnexion(db).cursor() as cur:
        lignes = pe._versions_courantes(cur, AUJOURDHUI)

    assert ("matisse", "0.13.1") in lignes, "matisse perdu alors qu'un alias le résout"
    ligne = lignes[("matisse", "0.13.1")]
    assert ligne["installations"] == 90
    assert ligne["installations_recentes"] == 42
    assert ligne["telechargements_cumules"] == 77
    # Le nom exporté reste le nom NEUTRE du contrat, jamais le slug du site.
    assert all(nom in ("libreoffice", "matisse") for nom, _ in lignes)


def test_plugin_introuvable_est_journalise_et_compte(caplog):
    """Un plugin du contrat qui ne se résout pas doit se VOIR, pas se déduire."""
    db = _base_avec_fixtures()
    db.plugins = {"mirai-libreoffice": 1}   # ni « matisse » ni « mirai-matisse »
    db.aliases = {}
    pe.PLUGINS_NON_RESOLUS.clear()

    with caplog.at_level("WARNING"):
        with FauxConnexion(db).cursor() as cur:
            lignes = pe._versions_courantes(cur, AUJOURDHUI)

    assert pe.PLUGINS_NON_RESOLUS.get("matisse") == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("matisse" in m and "mirai-matisse" in m and "PAS export" in m
               for m in messages), \
        f"le log doit nommer les DEUX candidats essayés, vu : {messages}"
    assert not any(nom == "matisse" for nom, _ in lignes)
    # Et l'écran de debug la remonte, c'est là que l'exploitant la verra.
    assert pe.PLUGINS_NON_RESOLUS == {"matisse": 1}
    pe.PLUGINS_NON_RESOLUS.clear()


def test_plugin_resolu_sans_version_reste_silencieux():
    """Catalogue créé mais rien de publié : cas légitime, aucun avertissement."""
    db = _base_avec_fixtures()          # mirai-matisse existe, catalogue vide
    pe.PLUGINS_NON_RESOLUS.clear()
    with FauxConnexion(db).cursor() as cur:
        pe._versions_courantes(cur, AUJOURDHUI)
    assert pe.PLUGINS_NON_RESOLUS == {}
