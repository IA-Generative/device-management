"""Tests — branches d'expérimentation : push cohortes (A), pull catalogue (B),
purge cache binaire (C).

Interactions DB mockées (pas de PostgreSQL réel, même approche que
test_campaign_plugin_scoping) : on vérifie le SQL émis, les paramètres transmis
et le comportement des fonctions pures.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import MagicMock


def _setup_env() -> None:
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_CONFIG_PROFILE"] = "prod"
    os.environ["DM_RELAY_ENABLED"] = "false"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DATABASE_URL"] = "postgresql://dev:dev@localhost:5432/bootstrap"


def _load_module():
    _setup_env()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.settings", None)
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = MagicMock()
    fake_psycopg2.Error = Exception
    sys.modules["psycopg2"] = fake_psycopg2
    mod = importlib.import_module("app.main")
    importlib.reload(mod)
    mod.psycopg2 = fake_psycopg2
    return mod


def _make_recording_cursor(rows_by_fragment: dict) -> MagicMock:
    cur = MagicMock()
    cur.calls = []
    _last = [""]

    def _execute(sql, params=None):
        _last[0] = sql
        cur.calls.append((sql, params))

    def _fetchall():
        for frag, rows in rows_by_fragment.items():
            if frag in _last[0]:
                return list(rows)
        return []

    def _fetchone():
        rows = _fetchall()
        return rows[0] if rows else None

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _find_call(cur, fragment: str):
    hits = [(sql, p) for sql, p in cur.calls if fragment in sql]
    assert hits, f"Aucune requête contenant {fragment!r}. Vu: {[s[:70] for s, _ in cur.calls]}"
    return hits[-1]


def _experiment_campaign(**over):
    base = {
        "campaign_id": 7,
        "urgency": "normal",
        "deadline_iso": None,
        "artifact_version": "1.6.0-rc1",
        "artifact_s3_path": "libreoffice/plugin-1.6.0-rc1.oxt",
        "artifact_checksum": "sha256:exp",
        "changelog_url": None,
        "rollback_s3_path": None,
        "rollback_version": None,
        "rollback_checksum": None,
        "rollout_config": None,
        "is_experiment": True,
    }
    base.update(over)
    return base


# ── A2 : précédence déterministe ─────────────────────────────────────────

def test_resolve_campaign_precedence_order_by():
    mod = _load_module()
    cur = _make_recording_cursor({"FROM campaigns c": []})
    mod._resolve_active_campaign(cur, device_cohort_ids=[1], device_type="libreoffice",
                                 platform_version="", plugin_id=7)
    sql, _ = _find_call(cur, "FROM campaigns c")
    assert "(c.target_cohort_id IS NOT NULL) DESC" in sql, "bras ciblé doit battre le rollout général"
    assert "c.priority DESC" in sql, "priority doit départager"
    assert "c.is_experiment" in sql, "is_experiment doit être sélectionné (mode pin)"


# ── A3 : mode pin ────────────────────────────────────────────────────────

def test_pin_serves_suffixed_target_even_when_not_greater():
    """1.6.0-rc1 se réduit à (0,) par _parse_version_tuple → sans pin, aucun
    update. Avec is_experiment, on sert la cible quand même."""
    mod = _load_module()
    d = mod._build_update_directive(plugin_version="1.5.0",
                                    campaign=_experiment_campaign(),
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d is not None
    assert d["action"] == "update"
    assert d["target_version"] == "1.6.0-rc1"


def test_pin_serves_lower_numbered_prototype():
    """Prototype dont le numéro est < courant : le pin le sert quand même
    (contourne la comparaison <)."""
    mod = _load_module()
    d = mod._build_update_directive(plugin_version="2.0.0",
                                    campaign=_experiment_campaign(artifact_version="1.9.0-exp"),
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d is not None
    assert d["action"] == "update"
    assert d["target_version"] == "1.9.0-exp"


def test_pin_none_when_already_on_target():
    mod = _load_module()
    d = mod._build_update_directive(plugin_version="1.6.0-rc1",
                                    campaign=_experiment_campaign(),
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d is None, "pas de re-update quand le device est déjà sur la version cible"


def test_non_experiment_still_requires_greater_version():
    """Régression : hors expé, une cible ancienne ne déclenche PAS d'update
    (sinon rollback si dispo). Ici pas de rollback → None."""
    mod = _load_module()
    camp = _experiment_campaign(is_experiment=False, artifact_version="1.0.0")
    d = mod._build_update_directive(plugin_version="2.0.0", campaign=camp,
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d is None


# ── A4 : l'URL servie est épinglée sur la version du bras ────────────────

def test_experiment_directive_url_is_pinned_not_latest_main():
    """Régression : /catalog/<slug>/download (sans tag) résout status='published',
    donc la version MAIN. Un bras d'expé qui l'utilisait annonçait 1.6.0-rc1 et
    servait le stable → checksum mismatch puis re-update à chaque poll."""
    mod = _load_module()
    d = mod._build_update_directive(plugin_version="1.5.0",
                                    campaign=_experiment_campaign(),
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d["artifact_url"] != "/catalog/mirai-libreoffice/download", \
        "l'URL générique sert la dernière version publiée, pas le bras testé"
    assert d["target_version"] in d["artifact_url"]


def test_experiment_directive_url_round_trips_to_target_version():
    """Bout en bout : l'URL produite, re-parsée par la route de téléchargement,
    doit sélectionner la version cible de la campagne."""
    mod = _load_module()
    d = mod._build_update_directive(plugin_version="1.5.0",
                                    campaign=_experiment_campaign(),
                                    client_uuid="u1", device_name="mirai-libreoffice")
    seen = {}
    mod._serve_variant_by_filename = lambda slug, filename: None
    mod._serve_plugin_download = lambda slug, version_filter=None: seen.update(
        slug=slug, version=version_filter)
    mod.catalog_download_file("mirai-libreoffice", d["artifact_url"].rsplit("/", 1)[-1])
    assert seen == {"slug": "mirai-libreoffice", "version": "1.6.0-rc1"}


def test_experiment_directive_url_falls_back_to_raw_binary_on_unknown_ext():
    """Extension que la route de téléchargement ne sait pas retirer → chemin brut,
    qui désigne l'artefact exact (plutôt qu'une URL que le parse casserait)."""
    mod = _load_module()
    camp = _experiment_campaign(artifact_s3_path="libreoffice/plugin-1.6.0-rc1.tar.gz")
    d = mod._build_update_directive(plugin_version="1.5.0", campaign=camp,
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d["artifact_url"] == "/binaries/libreoffice/plugin-1.6.0-rc1.tar.gz"


def test_general_campaign_url_unchanged():
    """Non-régression : hors expé, l'URL catalogue générique reste utilisée
    (la campagne générale sert justement la dernière version publiée)."""
    mod = _load_module()
    camp = _experiment_campaign(is_experiment=False, artifact_version="2.0.0")
    d = mod._build_update_directive(plugin_version="1.0.0", campaign=camp,
                                    client_uuid="u1", device_name="mirai-libreoffice")
    assert d["artifact_url"] == "/catalog/mirai-libreoffice/download"


# ── A1 : auto-complétion scopée ──────────────────────────────────────────

def _camp_svc():
    from app.admin.services import campaigns as camp_svc
    return camp_svc


def test_autocomplete_experiment_is_cohort_scoped():
    camp = _camp_svc()
    cur = _make_recording_cursor({"RETURNING id": [(99,)]})
    camp.create_campaign(cur, name="exp A", status="active", is_experiment=True,
                         target_cohort_id=5, plugin_id=7, artifact_id=3, priority=10)
    upd, params = _find_call(cur, "UPDATE campaigns SET status = 'completed'")
    assert "COALESCE(is_experiment, false) = %(is_exp)s" in upd
    assert "target_cohort_id IS NOT DISTINCT FROM %(cohort)s" in upd
    assert params["is_exp"] is True and params["cohort"] == 5 and params["pid"] == 7
    # L'INSERT porte les nouveaux champs
    ins, ip = _find_call(cur, "INSERT INTO campaigns")
    assert "is_experiment" in ins and "priority" in ins
    assert True in ip and 10 in ip


def test_autocomplete_general_supersedes_regardless_of_cohort():
    camp = _camp_svc()
    cur = _make_recording_cursor({"RETURNING id": [(100,)]})
    camp.create_campaign(cur, name="release", status="active", is_experiment=False,
                         target_cohort_id=None, plugin_id=7, artifact_id=3)
    upd, params = _find_call(cur, "UPDATE campaigns SET status = 'completed'")
    # is_exp=false → clause cohorte neutralisée (%(is_exp)s = false OR ...)
    assert "%(is_exp)s = false OR target_cohort_id IS NOT DISTINCT FROM" in upd
    assert "plugin_id IS NULL" in upd  # nettoyage legacy conservé
    assert params["is_exp"] is False


def test_update_status_activate_triggers_scoped_completion():
    camp = _camp_svc()
    cur = _make_recording_cursor({
        "SELECT plugin_id, COALESCE(is_experiment": [(7, True, 5)],
        "RETURNING id": [(1,)],
    })
    camp.update_campaign_status(cur, 1, "active")
    upd, params = _find_call(cur, "UPDATE campaigns SET status = 'completed'")
    assert "id <> %(self)s" in upd or "id <> %(exclude)s" in upd
    assert params.get("pid") == 7


def test_update_status_pause_does_not_autocomplete():
    camp = _camp_svc()
    cur = _make_recording_cursor({"RETURNING id": [(1,)]})
    camp.update_campaign_status(cur, 1, "paused")
    assert not any("SET status = 'completed'" in sql for sql, _ in cur.calls), \
        "une mise en pause ne doit rien auto-compléter"


# ── B1/B2 : servir une version expérimentale + download par tag ──────────

def _download_conn(cur):
    conn = MagicMock()
    conn.autocommit = True
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def test_serve_plugin_download_version_filter_allows_experimental():
    mod = _load_module()
    mod._pooled_conn = lambda: None
    cur = _make_recording_cursor({
        "FROM plugins WHERE slug": [(7, "libreoffice")],
        "AND pv.version = %s": [("0.9.14-exp", "managed", None, 123)],
        "FROM artifacts WHERE id": [("/data/content/binaries/x.oxt",)],
    })
    mod.psycopg2.connect = MagicMock(return_value=_download_conn(cur))
    from fastapi import HTTPException
    try:
        mod._serve_plugin_download("mirai-libreoffice", version_filter="0.9.14-exp")
    except HTTPException:
        pass  # le binaire mocké n'existe pas sur disque → 404, hors sujet ici
    sql, params = _find_call(cur, "AND pv.version = %s")
    assert "status IN ('published','experimental')" in sql
    assert params == (7, "0.9.14-exp")


def test_catalog_download_by_tag_resolves_experimental():
    mod = _load_module()
    mod._pooled_conn = lambda: None
    cur = _make_recording_cursor({
        "FROM plugins WHERE slug": [(7, "libreoffice")],
        "AND pv.tag = %s": [("0.9.14-exp",)],
    })
    mod.psycopg2.connect = MagicMock(return_value=_download_conn(cur))
    resp = mod.catalog_download("mirai-libreoffice", tag="exp-summary")
    # Redirige vers le fichier versionné de la branche taguée
    assert getattr(resp, "status_code", None) in (302, 307)
    loc = resp.headers.get("location", "")
    assert "0.9.14-exp" in loc
    sql, params = _find_call(cur, "AND pv.tag = %s")
    assert "status IN ('published','experimental')" in sql
    assert params == (7, "exp-summary")


def test_catalog_download_no_tag_stays_on_published():
    mod = _load_module()
    mod._pooled_conn = lambda: None
    cur = _make_recording_cursor({
        "FROM plugins WHERE slug": [(7, "libreoffice")],
        "pv.status = 'published'": [("1.0.0",)],
    })
    mod.psycopg2.connect = MagicMock(return_value=_download_conn(cur))
    resp = mod.catalog_download("mirai-libreoffice")
    loc = resp.headers.get("location", "")
    assert "1.0.0" in loc
    # aucune requête par tag
    assert not any("pv.tag = %s" in sql for sql, _ in cur.calls)


# ── C : purge / éviction du cache binaire ────────────────────────────────

def test_delete_binary_local_removes_file(tmp_path, monkeypatch):
    from app.services import binaries as b
    monkeypatch.setattr(b.settings, "binaries_mode", "local", raising=False)
    f = tmp_path / "plugin-x.oxt"
    f.write_bytes(b"data")
    assert b.delete_binary(str(f)) is True
    assert not f.exists()
    # idempotent : deuxième appel = False (déjà absent), pas d'exception
    assert b.delete_binary(str(f)) is False


def test_evict_orphan_cache_removes_only_orphans(tmp_path, monkeypatch):
    from app.services import binaries as b
    monkeypatch.setattr(b.settings, "binaries_mode", "local", raising=False)
    monkeypatch.setattr(b.settings, "local_binaries_dir", str(tmp_path), raising=False)
    sub = tmp_path / "libreoffice"
    sub.mkdir()
    live = sub / "plugin-1.0.0.oxt"
    orphan = sub / "plugin-0.9-exp.oxt"
    live.write_bytes(b"a")
    orphan.write_bytes(b"b")
    removed = b.evict_orphan_cache({"/data/content/binaries/libreoffice/plugin-1.0.0.oxt"})
    assert removed == 1
    assert live.exists() and not orphan.exists()
