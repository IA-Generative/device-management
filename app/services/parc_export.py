"""Export parc → bus de la bêta (suivi-beta) : agrégats d'usage des plugins.

Contrat (référence unique, côté bus identique) :

- destination ``PARC_EXPORT_URL`` : ``POST {base}/_parc/delta`` et
  ``POST {base}/_parc/instantane``, en-tête ``X-Parc-Secret``.
- fenêtre = jour courant UTC + veille ; valeurs ABSOLUES (jamais d'incréments),
  rejouer est sans effet.
- ``parc.delta.v1`` : lignes modifiées depuis le dernier seq ACQUITTÉ ; delta
  vide = battement (toujours envoyé). Réponses : 200 {"seq": n} ;
  409 {"resynchroniser": true} → instantané complet aussitôt, puis reprise des
  deltas ; 401/400 → journalisé + compteur, PAS de boucle.
- CLAUSE DE REJEU : un delta rejoué (même ``seq``, ``seq_precedente = seq-1``)
  est acquitté 200 par le bus SANS effet. Après un timeout/réponse perdue, on
  réémet donc le MÊME seq au cycle suivant (``etat.seq`` n'ayant pas avancé,
  ``seq_delta = etat.seq + 1`` retombe sur le même numéro) : le 200 reçu est
  traité normalement — jamais d'erreur ni de resynchronisation sur un rejeu.
  Le 409 n'arrive que sur chaînon manquant ou empreinte divergente.
- ``parc.instantane.v1`` : état complet de la fenêtre — au premier démarrage,
  sur 409, et en clôture de journée (premier cycle après minuit UTC).
- empreinte canonique (sha256) de TOUT l'état de la fenêtre, vérifiable côté bus.

Mécanique locale :

- agrégation INCRÉMENTALE par curseur sur ``device_telemetry_events.id`` (la
  table n'a ni index d'agrégation ni purge historique : jamais de re-scan
  complet) → table ``parc_agregat`` ; ``postes`` = COUNT(DISTINCT client_uuid)
  recalculé sur la fenêtre du jour pour les seules combinaisons touchées,
  sentinelles exclues.
- lignes « versions » (installations/canal/téléchargements) recalculées à
  chaque cycle et diffées contre ``parc_version_etat`` (miroir local de l'état
  connu du bus) pour ne pousser que les modifications.
- ``seq`` : ``parc_export_etat.seq`` = dernier seq ACQUITTÉ. Chaque ligne
  modifiée est marquée ``seq = acquitté + 1`` ; la sélection d'un delta est
  ``seq > acquitté`` — un envoi raté laisse les lignes « sales » et elles
  repartent au cycle suivant.
- multi-pods : verrou consultatif Postgres + garde « trop tôt » sur
  ``dernier_envoi`` (un seul export par intervalle, quel que soit le pod).
- journal des cycles : table ``parc_export_journal`` (bornée à ~50 lignes) —
  choisie contre un état mémoire process pour être visible de tous les pods et
  survivre aux redémarrages (elle alimente la section debug admin).

ANTI-FUITE : le payload est construit UNIQUEMENT depuis les agrégats
(``parc_agregat`` / ``parc_version_etat`` / compteurs) — jamais d'email,
client_uuid, IP, hostname ni attributs bruts. Verrouillé par test.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta

import httpx

from .db import get_db_connection
from .parc_mapping import (
    CLIENT_UUID_SENTINELLES,
    PLUGINS_EXPORTES,
    cles_pour,
    mapper_evenement,
)

logger = logging.getLogger("dm-parc-export")

SCHEMA_DELTA = "parc.delta.v1"
SCHEMA_INSTANTANE = "parc.instantane.v1"

# Bornes du contrat.
MAX_LIGNES_USAGE = 2000
MAX_CORPS_OCTETS = 256 * 1024
MAX_COMPTEUR = 10_000_000

# Codes locaux du journal (jamais renvoyés par le bus) :
CODE_RESEAU = 0        # amont injoignable / timeout
CODE_BORNES = -1       # payload hors bornes, non envoyé

# Verrou consultatif : un seul pod exporte à la fois.
_LOCK_ID = 727270920

# Agrégation : taille de lot et plafond de lots par cycle (le curseur persiste,
# le reliquat part au cycle suivant).
_LOT_EVENEMENTS = 5000
_MAX_LOTS_PAR_CYCLE = 20

# Rétention locale des tables d'agrégats (hygiène ; la fenêtre exportée ne
# couvre que J et J-1).
_RETENTION_AGREGATS_JOURS = 7
_JOURNAL_MAX_LIGNES = 50


def _maintenant() -> datetime:
    """Horloge UTC (isolée pour les tests)."""
    return datetime.now(UTC)


def borner(valeur) -> int:
    """Compteur du contrat : entier borné 0..10^7."""
    try:
        n = int(valeur or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_COMPTEUR, n))


def _iso_jour(j) -> str:
    if isinstance(j, date):
        return j.isoformat()
    return str(j or "")


# ── Empreinte canonique (identique côté bus) ─────────────────────────────────

def lignes_canoniques(versions: list[dict], usage: list[dict]) -> list[str]:
    """Lignes canoniques de TOUT l'état de la fenêtre, triées lexicographiquement."""
    lignes = []
    for v in versions:
        lignes.append(
            "v|{plugin}|{version}|{jour}|{canal}|{publiee_le}|{installations}"
            "|{installations_recentes}|{telechargements_cumules}".format(**v)
        )
    for u in usage:
        lignes.append(
            "u|{plugin}|{jour}|{version}|{fonction}|{appels}|{postes}|{erreurs}".format(**u)
        )
    return sorted(lignes)


def calculer_empreinte(versions: list[dict], usage: list[dict]) -> str:
    """sha256 hex du texte canonique (lignes triées jointes par \\n, UTF-8)."""
    texte = "\n".join(lignes_canoniques(versions, usage))
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


# ── Construction des payloads (pur, testable sans base) ──────────────────────

def _ligne_version(jour, plugin, version, canal, publiee_le,
                   installations, recentes, telechargements) -> dict:
    return {
        "plugin": str(plugin),
        "version": str(version),
        "jour": _iso_jour(jour),
        "canal": str(canal),
        "publiee_le": _iso_jour(publiee_le),
        "installations": borner(installations),
        "installations_recentes": borner(recentes),
        "telechargements_cumules": borner(telechargements),
    }


def _ligne_usage(jour, plugin, version, fonction, appels, postes, erreurs) -> dict:
    appels_b = borner(appels)
    return {
        "jour": _iso_jour(jour),
        "plugin": str(plugin),
        "version": str(version),
        "fonction": str(fonction),
        "appels": appels_b,
        "postes": borner(postes),
        # Invariant du contrat : erreurs ≤ appels.
        "erreurs": min(borner(erreurs), appels_b),
    }


def _entete(schema: str, seq: int, empreinte: str) -> dict:
    from ..settings import settings
    return {
        "schema": schema,
        "genere_le": _maintenant().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance": str(settings.parc_instance_label or "dm-x"),
        "seq": int(seq),
        "empreinte": empreinte,
    }


def construire_delta(*, seq: int, seq_precedente: int, empreinte: str,
                     versions: list[dict], usage: list[dict],
                     jours_clos: list[str]) -> dict:
    corps = _entete(SCHEMA_DELTA, seq, empreinte)
    corps["seq_precedente"] = int(seq_precedente)
    corps["versions"] = sorted(versions, key=lambda v: (v["plugin"], v["version"], v["jour"]))
    corps["usage"] = _tronquer_usage(sorted(
        usage, key=lambda u: (u["plugin"], u["jour"], u["version"], u["fonction"])))
    corps["jours_clos"] = sorted(jours_clos)
    return corps


def construire_instantane(*, seq: int, empreinte: str,
                          versions: list[dict], usage: list[dict]) -> dict:
    corps = _entete(SCHEMA_INSTANTANE, seq, empreinte)
    par_plugin: dict[str, dict] = {
        p: {"plugin": p, "versions": [], "usage": []} for p in sorted(PLUGINS_EXPORTES)
    }
    for v in sorted(versions, key=lambda v: (v["plugin"], v["version"], v["jour"])):
        par_plugin.setdefault(v["plugin"], {"plugin": v["plugin"], "versions": [], "usage": []})
        par_plugin[v["plugin"]]["versions"].append(v)
    for u in _tronquer_usage(sorted(
            usage, key=lambda u: (u["plugin"], u["jour"], u["version"], u["fonction"]))):
        par_plugin.setdefault(u["plugin"], {"plugin": u["plugin"], "versions": [], "usage": []})
        par_plugin[u["plugin"]]["usage"].append(u)
    corps["plugins"] = [par_plugin[p] for p in sorted(par_plugin)]
    return corps


def _tronquer_usage(usage: list[dict]) -> list[dict]:
    if len(usage) <= MAX_LIGNES_USAGE:
        return usage
    logger.warning("parc export: %d lignes usage > borne %d — troncature (le bus resynchronisera)",
                   len(usage), MAX_LIGNES_USAGE)
    return usage[:MAX_LIGNES_USAGE]


def serialiser(payload: dict) -> bytes | None:
    """JSON compact ; None si le corps dépasse la borne du contrat (256 Kio)."""
    corps = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(corps) > MAX_CORPS_OCTETS:
        logger.error("parc export: corps %d o > %d o — envoi refusé", len(corps), MAX_CORPS_OCTETS)
        return None
    return corps


# ── Agrégation pure (les événements arrivent de SQL, la logique est ici) ─────

def agreger_evenements(evenements, fenetre: set[date]):
    """Agrège un lot d'événements bruts → compteurs par combinaison.

    ``evenements`` : itérable de tuples (id, client_uuid, span_name, ts,
    attributes, plugin_version). Retourne (compteurs, dernier_id) où
    ``compteurs[(jour, plugin, version, fonction)] = [appels, erreurs]``.
    Les spans hors allowlist et hors fenêtre sont ignorés (le curseur avance
    quand même).
    """
    compteurs: dict[tuple, list[int]] = {}
    dernier_id = 0
    for (evt_id, _client_uuid, span_name, ts, attributes, version) in evenements:
        dernier_id = max(dernier_id, int(evt_id))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        jour = ts.astimezone(UTC).date()
        if jour not in fenetre:
            continue
        mappe = mapper_evenement(str(span_name or ""), attributes if isinstance(attributes, dict) else None)
        if mappe is None:
            continue
        plugin, fonction, est_erreur = mappe
        cle = (jour, plugin, str(version or ""), fonction)
        ligne = compteurs.setdefault(cle, [0, 0])
        ligne[0] += 1
        if est_erreur:
            ligne[1] += 1
    return compteurs, dernier_id


# ── Envoi HTTP (timeout court, jamais fatal) ─────────────────────────────────

_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def _envoyer(base_url: str, secret: str, route: str, corps: bytes) -> tuple[int, dict]:
    """POST {base}/_parc/{route}. Retourne (code, réponse JSON) ; code 0 = réseau."""
    url = f"{base_url.rstrip('/')}/_parc/{route}"
    try:
        reponse = httpx.post(
            url,
            content=corps,
            headers={"Content-Type": "application/json", "X-Parc-Secret": secret},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("parc export: %s injoignable: %s", route, str(exc)[:200])
        return CODE_RESEAU, {}
    try:
        donnees = reponse.json()
        if not isinstance(donnees, dict):
            donnees = {}
    except Exception:
        donnees = {}
    return int(reponse.status_code), donnees


# ── Accès base ───────────────────────────────────────────────────────────────

def _charger_etat(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO parc_export_etat (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        cur.execute(
            "SELECT curseur_dte, seq, dernier_envoi, dernier_code, resyncs, dernier_jour "
            "FROM parc_export_etat WHERE id = 1")
        row = cur.fetchone()
    conn.commit()
    return {
        "curseur_dte": int(row[0] or 0),
        "seq": int(row[1] or 0),
        "dernier_envoi": row[2],
        "dernier_code": row[3],
        "resyncs": int(row[4] or 0),
        "dernier_jour": row[5],
    }


def _agreger_increment(conn, curseur: int, marqueur: int, fenetre: set[date]) -> int:
    """Avance le curseur par lots ; upserte parc_agregat ; recalcule postes.

    Chaque lot est une transaction (upserts + postes + curseur ensemble) : un
    crash ne peut ni perdre ni compter deux fois un événement.
    Retourne le curseur final.
    """
    for _ in range(_MAX_LOTS_PAR_CYCLE):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, client_uuid, span_name, COALESCE(span_ts, created_at), "
                "       attributes, COALESCE(plugin_version, '') "
                "FROM device_telemetry_events WHERE id > %s ORDER BY id LIMIT %s",
                (curseur, _LOT_EVENEMENTS))
            lot = cur.fetchall()
            if not lot:
                conn.commit()
                break
            compteurs, dernier_id = agreger_evenements(lot, fenetre)
            for (jour, plugin, version, fonction), (appels, erreurs) in compteurs.items():
                cur.execute(
                    """
                    INSERT INTO parc_agregat (jour, plugin, version, fonction, appels, postes, erreurs, seq)
                    VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
                    ON CONFLICT (jour, plugin, version, fonction) DO UPDATE SET
                        appels = parc_agregat.appels + EXCLUDED.appels,
                        erreurs = parc_agregat.erreurs + EXCLUDED.erreurs,
                        seq = EXCLUDED.seq
                    """,
                    (jour, plugin, version, fonction, appels, erreurs, marqueur))
                # postes : COUNT(DISTINCT client_uuid) ABSOLU sur la fenêtre du
                # jour pour la combinaison touchée — sentinelles exclues. La clé
                # d'attribution est la même qu'au mapping : plugin.action sinon
                # span_name.
                cles = cles_pour(plugin, fonction)
                if cles:
                    cur.execute(
                        """
                        UPDATE parc_agregat SET postes = (
                            SELECT COUNT(DISTINCT dte.client_uuid)
                            FROM device_telemetry_events dte
                            WHERE (COALESCE(dte.span_ts, dte.created_at) AT TIME ZONE 'UTC')::date = %s
                              AND COALESCE(dte.plugin_version, '') = %s
                              AND COALESCE(dte.attributes->>'plugin.action', dte.span_name) = ANY(%s)
                              AND dte.client_uuid IS NOT NULL AND dte.client_uuid <> ''
                              AND NOT (dte.client_uuid = ANY(%s))
                        )
                        WHERE jour = %s AND plugin = %s AND version = %s AND fonction = %s
                        """,
                        (jour, version, list(cles), list(CLIENT_UUID_SENTINELLES),
                         jour, plugin, version, fonction))
            curseur = max(curseur, dernier_id)
            cur.execute("UPDATE parc_export_etat SET curseur_dte = %s WHERE id = 1", (curseur,))
        conn.commit()
        if len(lot) < _LOT_EVENEMENTS:
            break
    return curseur


def _canal(status: str, maturity: str) -> str:
    """Contrat : published → release si maturité release sinon beta ;
    yanked/deprecated → retire. (draft/experimental : jamais exportés.)"""
    if status == "published":
        return "release" if (maturity or "") == "release" else "beta"
    return "retire"


# Plugins que l'export n'a pas su résoudre, par nom d'export → nombre de cycles.
# Remonté par etat_pour_debug() : une disparition doit se VOIR, pas se déduire.
PLUGINS_NON_RESOLUS: dict[str, int] = {}


def _resoudre_plugin(cur, nom_export: str, slug_canonique: str) -> tuple[list[int], list[str]]:
    """Résout un plugin du contrat par slug OU alias, sur DEUX candidats.

    L'erreur d'origine n'était pas le mécanisme de résolution mais CE QU'ON
    RÉSOLVAIT : ``PLUGINS_EXPORTES`` code un slug canonique *présumé*, et cette
    présomption peut être fausse. Sur le DGX (relevé du 2026-09-05), le catalogue
    porte ``matisse`` comme slug et ``mirai-matisse`` n'existe **nulle part** —
    ni slug, ni alias. Chercher le seul slug canonique y perdait le plugin
    ENTIER, en silence : 6 versions et 90 installations sur 120 jamais exportées.

    On essaie donc les deux candidats — le **nom d'export** et le slug canonique —
    chacun par slug OU par alias, comme le fait déjà ``app/main.py`` pour les
    devices. Sur ce même DGX, ``libreoffice`` tombe par alias sur
    ``mirai-libreoffice`` et ``matisse`` est un slug direct : aucun cas particulier
    n'a besoin d'être codé en dur, ni maintenant ni au prochain plugin renommé.

    Rend (ids de plugins, slugs réels) : les ids servent aux jointures, les slugs
    à ``download_events``, qui porte une colonne dénormalisée.
    """
    candidats = list(dict.fromkeys([nom_export, slug_canonique]))
    cur.execute(
        """
        SELECT p.id, p.slug
        FROM plugins p
        WHERE p.status <> 'removed'
          AND (p.slug = ANY(%s)
               OR p.id IN (SELECT plugin_id FROM plugin_aliases WHERE alias = ANY(%s)))
        """, (candidats, candidats))
    lignes = cur.fetchall()
    return [int(i) for (i, _) in lignes], [str(sl) for (_, sl) in lignes]


def _versions_courantes(cur, aujourdhui: date) -> dict[tuple, dict]:
    """Lignes « versions » recalculées (jour = jour courant), par (plugin, version)."""
    lignes: dict[tuple, dict] = {}
    for nom_export, slug in PLUGINS_EXPORTES.items():
        ids, slugs_reels = _resoudre_plugin(cur, nom_export, slug)
        if not ids:
            # Un plugin du contrat qui ne se résout pas est une PERTE DE DONNÉES,
            # pas un cas normal : on le dit, et on le compte pour /admin/debug.
            PLUGINS_NON_RESOLUS[nom_export] = PLUGINS_NON_RESOLUS.get(nom_export, 0) + 1
            # Nommer les DEUX candidats : c'est l'écart entre eux qui est la panne.
            logger.warning(
                "parc export: plugin « %s » introuvable — ni « %s » ni « %s » ne "
                "correspond à un slug ou un alias du catalogue ; il n'est PAS "
                "exporté ; %d cycle(s) dans cet état",
                nom_export, nom_export, slug, PLUGINS_NON_RESOLUS[nom_export])
            continue
        PLUGINS_NON_RESOLUS.pop(nom_export, None)
        cur.execute(
            """
            SELECT pv.version, pv.status, pv.published_at::date, p.maturity
            FROM plugin_versions pv JOIN plugins p ON p.id = pv.plugin_id
            WHERE pv.plugin_id = ANY(%s)
              AND pv.status IN ('published', 'deprecated', 'yanked')
            """, (ids,))
        versions = {str(v): (s, pub, m) for (v, s, pub, m) in cur.fetchall()}
        if not versions:
            # Plugin résolu mais aucune version publiable : cas légitime (catalogue
            # créé, rien encore publié). Silencieux à dessein.
            continue
        cur.execute(
            """
            SELECT COALESCE(pi.installed_version, ''),
                   COUNT(DISTINCT pi.client_uuid) FILTER (
                       WHERE pi.last_seen_at >= now() - interval '30 days'),
                   COUNT(DISTINCT pi.client_uuid) FILTER (
                       WHERE pi.last_seen_at >= now() - interval '7 days')
            FROM plugin_installations pi
            WHERE pi.plugin_id = ANY(%s) AND pi.status <> 'uninstalled'
              AND NOT (pi.client_uuid = ANY(%s))
            GROUP BY 1
            """, (ids, list(CLIENT_UUID_SENTINELLES)))
        installations = {str(v): (int(n30), int(n7)) for (v, n30, n7) in cur.fetchall()}
        cur.execute(
            "SELECT version_tag, COUNT(*) FROM download_events "
            "WHERE plugin_slug = ANY(%s) GROUP BY 1",
            (slugs_reels,))
        telechargements = {str(v): int(n) for (v, n) in cur.fetchall()}
        for version, (status, publiee_le, maturity) in versions.items():
            n30, n7 = installations.get(version, (0, 0))
            lignes[(nom_export, version)] = _ligne_version(
                aujourdhui, nom_export, version, _canal(status, maturity), publiee_le,
                n30, n7, telechargements.get(version, 0))
    return lignes


def _maj_etat_versions(conn, marqueur: int, aujourdhui: date) -> None:
    """Diffe les lignes versions recalculées contre parc_version_etat (jour
    courant) : upsert des modifiées (marquées seq=marqueur), suppression des
    disparues (le bus s'en apercevra par l'empreinte → 409 → instantané)."""
    with conn.cursor() as cur:
        courantes = _versions_courantes(cur, aujourdhui)
        cur.execute(
            "SELECT plugin, version, canal, publiee_le, installations, "
            "       installations_recentes, telechargements_cumules "
            "FROM parc_version_etat WHERE jour = %s", (aujourdhui,))
        existantes = {
            (p, v): (c, _iso_jour(pub), int(i), int(r), int(t))
            for (p, v, c, pub, i, r, t) in cur.fetchall()
        }
        for (plugin, version), ligne in courantes.items():
            cible = (ligne["canal"], ligne["publiee_le"], ligne["installations"],
                     ligne["installations_recentes"], ligne["telechargements_cumules"])
            if existantes.get((plugin, version)) == cible:
                continue
            cur.execute(
                """
                INSERT INTO parc_version_etat
                    (jour, plugin, version, canal, publiee_le, installations,
                     installations_recentes, telechargements_cumules, seq)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (jour, plugin, version) DO UPDATE SET
                    canal = EXCLUDED.canal,
                    publiee_le = EXCLUDED.publiee_le,
                    installations = EXCLUDED.installations,
                    installations_recentes = EXCLUDED.installations_recentes,
                    telechargements_cumules = EXCLUDED.telechargements_cumules,
                    seq = EXCLUDED.seq
                """,
                (aujourdhui, plugin, version, ligne["canal"], ligne["publiee_le"] or None,
                 ligne["installations"], ligne["installations_recentes"],
                 ligne["telechargements_cumules"], marqueur))
        for (plugin, version) in set(existantes) - set(courantes):
            cur.execute(
                "DELETE FROM parc_version_etat WHERE jour = %s AND plugin = %s AND version = %s",
                (aujourdhui, plugin, version))
    conn.commit()


def _etat_fenetre(conn, fenetre: list[date]) -> tuple[list[dict], list[dict]]:
    """TOUT l'état de la fenêtre (pour l'empreinte et l'instantané)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT jour, plugin, version, canal, publiee_le, installations, "
            "       installations_recentes, telechargements_cumules "
            "FROM parc_version_etat WHERE jour = ANY(%s)", (fenetre,))
        versions = [
            _ligne_version(j, p, v, c, pub, i, r, t)
            for (j, p, v, c, pub, i, r, t) in cur.fetchall()
        ]
        cur.execute(
            "SELECT jour, plugin, version, fonction, appels, postes, erreurs "
            "FROM parc_agregat WHERE jour = ANY(%s)", (fenetre,))
        usage = [
            _ligne_usage(j, p, v, f, a, po, e)
            for (j, p, v, f, a, po, e) in cur.fetchall()
        ]
    return versions, usage


def _lignes_modifiees(conn, seq_acquitte: int, fenetre: list[date]) -> tuple[list[dict], list[dict]]:
    """Lignes de la fenêtre modifiées depuis le dernier seq acquitté."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT jour, plugin, version, canal, publiee_le, installations, "
            "       installations_recentes, telechargements_cumules "
            "FROM parc_version_etat WHERE jour = ANY(%s) AND seq > %s",
            (fenetre, seq_acquitte))
        versions = [
            _ligne_version(j, p, v, c, pub, i, r, t)
            for (j, p, v, c, pub, i, r, t) in cur.fetchall()
        ]
        cur.execute(
            "SELECT jour, plugin, version, fonction, appels, postes, erreurs "
            "FROM parc_agregat WHERE jour = ANY(%s) AND seq > %s",
            (fenetre, seq_acquitte))
        usage = [
            _ligne_usage(j, p, v, f, a, po, e)
            for (j, p, v, f, a, po, e) in cur.fetchall()
        ]
    return versions, usage


def _purger_local(conn, aujourdhui: date) -> None:
    """Hygiène : agrégats hors fenêtre depuis longtemps (jamais réexportés)."""
    limite = aujourdhui - timedelta(days=_RETENTION_AGREGATS_JOURS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM parc_agregat WHERE jour < %s", (limite,))
        cur.execute("DELETE FROM parc_version_etat WHERE jour < %s", (limite,))
    conn.commit()


def _journaliser(conn, *, type_envoi: str, seq: int, lignes: int, code: int | None,
                 duree_ms: int) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO parc_export_journal (type, seq, lignes, code, duree_ms) "
                "VALUES (%s, %s, %s, %s, %s)",
                (type_envoi, seq, lignes, code, duree_ms))
            cur.execute(
                "DELETE FROM parc_export_journal WHERE id NOT IN ("
                "  SELECT id FROM parc_export_journal ORDER BY id DESC LIMIT %s)",
                (_JOURNAL_MAX_LIGNES,))
        conn.commit()
    except Exception:
        logger.debug("parc export: écriture du journal échouée", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass


def _marquer_envoi(conn, *, code: int, acquitte_seq: int | None = None,
                   resync: bool = False, jour_acquitte: date | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE parc_export_etat SET dernier_envoi = now(), dernier_code = %s, "
            "resyncs = resyncs + %s WHERE id = 1",
            (code, 1 if resync else 0))
        if acquitte_seq is not None:
            cur.execute("UPDATE parc_export_etat SET seq = %s WHERE id = 1", (acquitte_seq,))
        if jour_acquitte is not None:
            cur.execute("UPDATE parc_export_etat SET dernier_jour = %s WHERE id = 1",
                        (jour_acquitte,))
    conn.commit()


# ── Cycle ────────────────────────────────────────────────────────────────────

def _envoyer_instantane(conn, *, base_url: str, secret: str, seq: int,
                        fenetre: list[date], t0: float) -> int:
    versions, usage = _etat_fenetre(conn, fenetre)
    empreinte = calculer_empreinte(versions, usage)
    corps = serialiser(construire_instantane(
        seq=seq, empreinte=empreinte, versions=versions, usage=usage))
    if corps is None:
        _journaliser(conn, type_envoi="instantane", seq=seq,
                     lignes=len(versions) + len(usage), code=CODE_BORNES,
                     duree_ms=int((time.monotonic() - t0) * 1000))
        return CODE_BORNES
    code, _ = _envoyer(base_url, secret, "instantane", corps)
    if code == 200:
        _marquer_envoi(conn, code=code, acquitte_seq=seq)
    else:
        _marquer_envoi(conn, code=code)
    _journaliser(conn, type_envoi="instantane", seq=seq,
                 lignes=len(versions) + len(usage), code=code,
                 duree_ms=int((time.monotonic() - t0) * 1000))
    return code


def executer_cycle(*, force: bool = False) -> dict:
    """Un cycle complet : agrégation incrémentale + envoi (instantané si dû,
    puis delta — battement compris). Ne lève jamais ; retourne un résumé."""
    from ..settings import settings

    if not force and not bool(settings.parc_export_enabled):
        return {"statut": "desactive"}
    base_url = str(settings.parc_export_url or "").strip()
    if not base_url:
        return {"statut": "non_configure"}
    secret = str(settings.parc_export_secret or "")
    intervalle = max(60, int(settings.parc_export_intervalle_s or 300))

    conn = get_db_connection()
    if conn is None:
        return {"statut": "pas_de_base"}
    verrou = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
            verrou = bool(cur.fetchone()[0])
        conn.commit()
        if not verrou:
            return {"statut": "verrouille"}

        t0 = time.monotonic()
        etat = _charger_etat(conn)
        maintenant = _maintenant()
        aujourdhui = maintenant.date()
        fenetre = [aujourdhui, aujourdhui - timedelta(days=1)]

        # Garde multi-pods : chaque réplique tente le cycle, mais un envoi par
        # intervalle suffit (le battement reste dû, jamais doublé).
        if not force and etat["dernier_envoi"] is not None:
            age = (maintenant - etat["dernier_envoi"].astimezone(UTC)).total_seconds()
            if age < 0.8 * intervalle:
                return {"statut": "trop_tot"}

        marqueur = etat["seq"] + 1
        _agreger_increment(conn, etat["curseur_dte"], marqueur, set(fenetre))
        _maj_etat_versions(conn, marqueur, aujourdhui)
        _purger_local(conn, aujourdhui)

        premier = etat["seq"] == 0 and etat["dernier_envoi"] is None
        cloture = etat["dernier_jour"] is not None and etat["dernier_jour"] < aujourdhui
        jours_clos: list[str] = []
        if cloture:
            # Jours sortis de la fenêtre depuis le dernier delta acquitté
            # (typiquement [J-2] au premier cycle après minuit UTC).
            j = etat["dernier_jour"] - timedelta(days=1)
            while j < aujourdhui - timedelta(days=1):
                jours_clos.append(j.isoformat())
                j += timedelta(days=1)

        if premier or cloture:
            code = _envoyer_instantane(conn, base_url=base_url, secret=secret,
                                       seq=etat["seq"] + 1, fenetre=fenetre, t0=t0)
            if code != 200:
                # 401/400/réseau : journalisé, pas de boucle — on retentera au
                # prochain cycle.
                return {"statut": "instantane_refuse", "code": code}
            etat = _charger_etat(conn)

        # Delta (toujours envoyé — vide = battement).
        versions_mod, usage_mod = _lignes_modifiees(conn, etat["seq"], fenetre)
        versions_tout, usage_tout = _etat_fenetre(conn, fenetre)
        empreinte = calculer_empreinte(versions_tout, usage_tout)
        seq_delta = etat["seq"] + 1
        delta = construire_delta(
            seq=seq_delta, seq_precedente=etat["seq"], empreinte=empreinte,
            versions=versions_mod, usage=usage_mod, jours_clos=jours_clos)
        corps = serialiser(delta)
        nb_lignes = len(versions_mod) + len(usage_mod)
        if corps is None:
            _journaliser(conn, type_envoi="delta", seq=seq_delta, lignes=nb_lignes,
                         code=CODE_BORNES, duree_ms=int((time.monotonic() - t0) * 1000))
            return {"statut": "hors_bornes"}
        code, reponse = _envoyer(base_url, secret, "delta", corps)
        if code == 200:
            _marquer_envoi(conn, code=code, acquitte_seq=seq_delta, jour_acquitte=aujourdhui)
        elif code == 409 and reponse.get("resynchroniser"):
            # Le bus demande une resynchronisation : instantané complet
            # aussitôt (seq suivant), puis les deltas reprennent au prochain
            # cycle.
            _marquer_envoi(conn, code=code, resync=True)
            _journaliser(conn, type_envoi="delta", seq=seq_delta, lignes=nb_lignes,
                         code=code, duree_ms=int((time.monotonic() - t0) * 1000))
            code_resync = _envoyer_instantane(conn, base_url=base_url, secret=secret,
                                             seq=seq_delta + 1, fenetre=fenetre, t0=t0)
            if code_resync == 200:
                _marquer_envoi(conn, code=code_resync, jour_acquitte=aujourdhui)
            return {"statut": "resynchronise", "code": code_resync, "seq": seq_delta + 1}
        else:
            # 401/400/réseau : journaliser + compteur (dernier_code), NE PAS
            # boucler — les lignes restent « sales », et le cycle suivant
            # réémet le MÊME seq (clause de rejeu : si le bus avait en fait
            # reçu cet envoi, il ré-acquitte 200 sans effet).
            _marquer_envoi(conn, code=code)
        _journaliser(conn, type_envoi="delta", seq=seq_delta, lignes=nb_lignes,
                     code=code, duree_ms=int((time.monotonic() - t0) * 1000))
        return {"statut": "envoye" if code == 200 else "refuse",
                "code": code, "seq": seq_delta, "lignes": nb_lignes}
    except Exception:
        logger.exception("parc export: cycle en échec")
        try:
            conn.rollback()
        except Exception:
            pass
        return {"statut": "erreur"}
    finally:
        try:
            if verrou:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
                conn.commit()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ── État pour la page debug admin ────────────────────────────────────────────

def etat_pour_debug() -> dict:
    """Résumé pour la section « Export parc » de /admin/debug (best-effort)."""
    from ..settings import settings
    resume: dict = {
        "actif": bool(settings.parc_export_enabled),
        "configure": bool(str(settings.parc_export_url or "").strip()),
        "instance": str(settings.parc_instance_label or "dm-x"),
        "intervalle_s": int(settings.parc_export_intervalle_s or 300),
        "etat": None,
        "journal": [],
        "echecs_24h": 0,
        # Plugins du contrat que l'export ne résout pas : chacun est un pan du
        # parc qui ne sort pas. Vide = tout le catalogue est couvert.
        "plugins_non_resolus": dict(PLUGINS_NON_RESOLUS),
    }
    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return resume
        with conn.cursor() as cur:
            cur.execute(
                "SELECT curseur_dte, seq, dernier_envoi, dernier_code, resyncs, dernier_jour "
                "FROM parc_export_etat WHERE id = 1")
            row = cur.fetchone()
            if row:
                resume["etat"] = {
                    "curseur_dte": int(row[0] or 0),
                    "seq": int(row[1] or 0),
                    "dernier_envoi": row[2],
                    "dernier_code": row[3],
                    "resyncs": int(row[4] or 0),
                    "dernier_jour": row[5],
                }
            cur.execute(
                "SELECT heure, type, seq, lignes, code, duree_ms "
                "FROM parc_export_journal ORDER BY id DESC LIMIT 10")
            resume["journal"] = [
                {"heure": h, "type": t, "seq": s, "lignes": li, "code": c, "duree_ms": d}
                for (h, t, s, li, c, d) in cur.fetchall()
            ]
            cur.execute(
                "SELECT COUNT(*) FROM parc_export_journal "
                "WHERE heure > now() - interval '24 hours' AND (code IS NULL OR code <> 200)")
            resume["echecs_24h"] = int(cur.fetchone()[0] or 0)
    except Exception:
        logger.debug("parc export: état debug indisponible", exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return resume


# ── Boucle de fond (patron llm/traffic + runtime_config) ─────────────────────

_TIC_SECONDES = 15.0
_derniere_tentative = 0.0


def boucle_de_fond(stop_event: threading.Event) -> None:  # pragma: no cover - boucle, testée via executer_cycle
    """Réveil toutes les ~15 s ; cycle quand l'intervalle (hot-reloadable) est
    écoulé et que l'export est actif. La garde « trop tôt » du cycle évite les
    doublons multi-pods."""
    global _derniere_tentative
    logger.info("parc export: boucle de fond démarrée")
    while not stop_event.is_set():
        stop_event.wait(_TIC_SECONDES)
        if stop_event.is_set():
            break
        try:
            from ..settings import settings
            if not bool(settings.parc_export_enabled):
                continue
            intervalle = max(60, int(settings.parc_export_intervalle_s or 300))
            if time.monotonic() - _derniere_tentative < intervalle:
                continue
            _derniere_tentative = time.monotonic()
            executer_cycle()
        except Exception:
            logger.exception("parc export: itération de fond en échec")
    logger.info("parc export: boucle de fond arrêtée")


def demarrer_fond(stop_event: threading.Event) -> threading.Thread:
    t = threading.Thread(target=boucle_de_fond, args=(stop_event,),
                         daemon=True, name="dm-parc-export")
    t.start()
    return t
