"""Intégration (vrai Postgres) — coexistence et précédence des campagnes 0.9.14.

Pourquoi ce module : la règle métier des branches d'expérimentation est du **SQL**
(`autocomplete_superseded`, le WHERE/ORDER BY de `_resolve_active_campaign`, le
CHECK élargi de `plugin_versions.status`). Les tests unitaires de
`test_experiment_versions.py` mockent psycopg2 : ils vérifient le *texte* des
requêtes, donc ils passeraient avec un SQL syntaxiquement valide mais
sémantiquement faux. Ici, Postgres est juge.

Marqué `integration` (exclu du gate unitaire). Tout se déroule dans une
transaction annulée en sortie : aucune donnée ne survit au test.

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_experiment_campaigns_pg.py -m integration`
"""
from __future__ import annotations

import json
import os
import sys

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "db", "schema.sql")

_SLUG = "it-exp-plugin"
_DEVICE_TYPE = "it-exp-plugin"


def _load_main():
    """Charge app.main avec le VRAI psycopg2 (d'autres modules en injectent un faux)."""
    os.environ.setdefault("DM_STORE_ENROLL_LOCALLY", "false")
    os.environ.setdefault("DM_STORE_ENROLL_S3", "false")
    os.environ.setdefault("DM_CONFIG_ENABLED", "true")
    os.environ.setdefault("DM_AUTH_VERIFY_ACCESS_TOKEN", "false")
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    sys.modules.pop("psycopg2", None)
    import psycopg2  # le vrai module
    for name in ("app.main", "app.settings"):
        sys.modules.pop(name, None)
    import app.main as mod
    mod.psycopg2 = psycopg2
    return mod


@pytest.fixture(scope="module")
def _db_url() -> str:
    pytest.importorskip("psycopg2")
    url = os.getenv("DATABASE_ADMIN_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini")
    import psycopg2
    try:
        psycopg2.connect(url, connect_timeout=3).close()
    except Exception as exc:  # pragma: no cover — dépend de l'infra locale
        pytest.skip(f"Postgres injoignable: {exc}")
    # Garantit les colonnes 0.9.14 (is_experiment, priority, tag, hypotheses)
    # sur une base préexistante : c'est le fixup d'apply_schema qui migre.
    from app.services.db import apply_schema
    apply_schema(url, _SCHEMA_PATH)
    return url


@pytest.fixture
def fixt(_db_url):
    """Transaction annulée en sortie + jeu de données minimal (plugin, artefacts,
    cohortes). Renvoie (cur, ids)."""
    import psycopg2
    conn = psycopg2.connect(_db_url)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO plugins (slug, name, device_type, status)
        VALUES (%s, 'IT expérimentation', %s, 'active') RETURNING id
    """, (_SLUG, _DEVICE_TYPE))
    plugin_id = cur.fetchone()[0]

    def _artifact(version: str) -> int:
        cur.execute("""
            INSERT INTO artifacts (device_type, version, s3_path, checksum, is_active)
            VALUES (%s, %s, %s, %s, true) RETURNING id
        """, (_DEVICE_TYPE, version, f"{_DEVICE_TYPE}/plugin-{version}.oxt", f"sha256:{version}"))
        return cur.fetchone()[0]

    def _cohort(name: str) -> int:
        cur.execute("""
            INSERT INTO cohorts (name, type, config) VALUES (%s, 'manual', '{}'::jsonb)
            RETURNING id
        """, (name,))
        return cur.fetchone()[0]

    ids = {
        "plugin_id": plugin_id,
        "stable": _artifact("1.5.0"),
        "rc": _artifact("1.6.0-rc1"),
        "next_stable": _artifact("1.7.0"),
        "cohort_a": _cohort("IT testeurs A"),
        "cohort_b": _cohort("IT testeurs B"),
    }
    try:
        yield cur, ids
    finally:
        conn.rollback()
        cur.close()
        conn.close()


def _status(cur, campaign_id: int) -> str:
    cur.execute("SELECT status FROM campaigns WHERE id = %s", (campaign_id,))
    return cur.fetchone()[0]


def _svc():
    from app.admin.services import campaigns as camp_svc
    return camp_svc


def _new(cur, ids, **over) -> int:
    kw = {"name": "IT campagne", "status": "active", "plugin_id": ids["plugin_id"],
          "artifact_id": ids["stable"], "target_cohort_id": None,
          "is_experiment": False}
    kw.update(over)
    return _svc().create_campaign(cur, **kw)


# ── Coexistence : une expé et le rollout général vivent ensemble ─────────────

def test_experiment_and_general_rollout_coexist(fixt):
    cur, ids = fixt
    general = _new(cur, ids, artifact_id=ids["stable"])
    exp = _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
               target_cohort_id=ids["cohort_a"], priority=10)
    assert _status(cur, general) == "active", "l'expé ne doit pas compléter le rollout"
    assert _status(cur, exp) == "active"


def test_new_general_release_supersedes_general_but_spares_experiment(fixt):
    cur, ids = fixt
    old_general = _new(cur, ids, artifact_id=ids["stable"])
    exp = _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
               target_cohort_id=ids["cohort_a"])
    new_general = _new(cur, ids, artifact_id=ids["next_stable"])
    assert _status(cur, old_general) == "completed", "sémantique historique perdue"
    assert _status(cur, exp) == "active", "une release générale ne doit JAMAIS tuer une expé"
    assert _status(cur, new_general) == "active"


def test_new_experiment_supersedes_only_its_own_arm(fixt):
    cur, ids = fixt
    general = _new(cur, ids, artifact_id=ids["stable"])
    arm_a1 = _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
                  target_cohort_id=ids["cohort_a"])
    arm_b = _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
                 target_cohort_id=ids["cohort_b"])
    arm_a2 = _new(cur, ids, artifact_id=ids["next_stable"], is_experiment=True,
                  target_cohort_id=ids["cohort_a"])
    assert _status(cur, arm_a1) == "completed", "re-déploiement du bras A : l'ancien sort"
    assert _status(cur, arm_b) == "active", "le bras B (autre cohorte) doit survivre"
    assert _status(cur, general) == "active", "le rollout général doit survivre"
    assert _status(cur, arm_a2) == "active"


def test_activate_transition_applies_the_same_scoping(fixt):
    """activate/resume ne passe pas par create_campaign : la garde doit y être aussi."""
    cur, ids = fixt
    general = _new(cur, ids, artifact_id=ids["stable"])
    exp = _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
               target_cohort_id=ids["cohort_a"])
    draft = _new(cur, ids, artifact_id=ids["next_stable"], status="draft")
    assert _svc().update_campaign_status(cur, draft, "active") is True
    assert _status(cur, general) == "completed"
    assert _status(cur, exp) == "active"
    assert _status(cur, draft) == "active"


# ── Précédence : quelle campagne un device reçoit-il ? ───────────────────────

def test_precedence_targeted_arm_beats_general_rollout(fixt):
    cur, ids = fixt
    _new(cur, ids, artifact_id=ids["stable"])
    _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
         target_cohort_id=ids["cohort_a"], priority=10)
    mod = _load_main()

    inside = mod._resolve_active_campaign(
        cur, device_cohort_ids=[ids["cohort_a"]], device_type=_DEVICE_TYPE,
        platform_version="", plugin_id=ids["plugin_id"])
    assert inside is not None, "aucune campagne résolue (SQL en échec ?)"
    assert inside["artifact_version"] == "1.6.0-rc1", "le testeur doit recevoir le bras"
    assert inside["is_experiment"] is True

    outside = mod._resolve_active_campaign(
        cur, device_cohort_ids=[], device_type=_DEVICE_TYPE,
        platform_version="", plugin_id=ids["plugin_id"])
    assert outside is not None
    assert outside["artifact_version"] == "1.5.0", "hors cohorte = témoin sur le stable"
    assert outside["is_experiment"] is False


def test_precedence_priority_breaks_the_tie_between_arms(fixt):
    """Deux bras ciblés que le device matche : priority tranche, pas created_at."""
    cur, ids = fixt
    _new(cur, ids, artifact_id=ids["next_stable"], is_experiment=True,
         target_cohort_id=ids["cohort_b"], priority=50)
    _new(cur, ids, artifact_id=ids["rc"], is_experiment=True,
         target_cohort_id=ids["cohort_a"], priority=1)   # plus récente, priorité basse
    mod = _load_main()
    got = mod._resolve_active_campaign(
        cur, device_cohort_ids=[ids["cohort_a"], ids["cohort_b"]],
        device_type=_DEVICE_TYPE, platform_version="", plugin_id=ids["plugin_id"])
    assert got is not None
    assert got["artifact_version"] == "1.7.0", "priority doit primer sur created_at"


def test_campaign_of_another_plugin_is_never_served(fixt):
    """Non-régression issue #14 : le scoping par plugin tient aussi pour une expé."""
    cur, ids = fixt
    cur.execute("""
        INSERT INTO plugins (slug, name, device_type, status)
        VALUES ('it-exp-other', 'IT autre', 'it-exp-other', 'active') RETURNING id
    """)
    other_plugin = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO artifacts (device_type, version, s3_path, checksum, is_active)
        VALUES ('it-exp-other', '9.9.9', 'it-exp-other/x.oxt', 'sha256:x', true) RETURNING id
    """)
    other_artifact = cur.fetchone()[0]
    _svc().create_campaign(cur, name="IT autre plugin", status="active",
                           plugin_id=other_plugin, artifact_id=other_artifact,
                           is_experiment=True, target_cohort_id=ids["cohort_a"])
    mod = _load_main()
    got = mod._resolve_active_campaign(
        cur, device_cohort_ids=[ids["cohort_a"]], device_type=_DEVICE_TYPE,
        platform_version="", plugin_id=ids["plugin_id"])
    assert got is None, "la campagne d'un autre plugin ne doit jamais fuiter"


# ── Le CHECK élargi accepte 'experimental' et rien de plus ──────────────────

def test_plugin_versions_status_check_accepts_experimental_only(fixt):
    import psycopg2
    cur, ids = fixt
    cur.execute("""
        INSERT INTO plugin_versions (plugin_id, version, status, tag, hypotheses)
        VALUES (%s, '1.6.0-rc1', 'experimental', 'exp-it', '["question ?"]'::jsonb)
        RETURNING status, tag
    """, (ids["plugin_id"],))
    assert cur.fetchone() == ("experimental", "exp-it")

    cur.execute("SAVEPOINT sp_check")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
            INSERT INTO plugin_versions (plugin_id, version, status)
            VALUES (%s, '1.6.0-rc2', 'bidon')
        """, (ids["plugin_id"],))
    cur.execute("ROLLBACK TO SAVEPOINT sp_check")


# ── Étanchéité des surfaces publiques ────────────────────────────────────────

_SEAL_SLUG = "it-seal-plugin"
_SEAL_TAG = "it-seal-branche"


@pytest.fixture
def sealed(_db_url):
    """Jeu de données COMMITÉ : les routes publiques ouvrent leur propre connexion,
    elles ne verraient pas une transaction en cours. Nettoyage en sortie."""
    import psycopg2
    conn = psycopg2.connect(_db_url)
    conn.autocommit = True

    def _purge(cur):
        cur.execute(f"DELETE FROM plugin_versions WHERE plugin_id IN "
                    f"(SELECT id FROM plugins WHERE slug = '{_SEAL_SLUG}')")
        cur.execute(f"DELETE FROM artifacts WHERE device_type = '{_SEAL_SLUG}'")
        cur.execute(f"DELETE FROM plugins WHERE slug = '{_SEAL_SLUG}'")

    with conn.cursor() as cur:
        _purge(cur)
        cur.execute("""INSERT INTO plugins (slug, name, device_type, status, visibility)
                       VALUES (%s, 'IT etancheite', %s, 'active', 'public') RETURNING id""",
                    (_SEAL_SLUG, _SEAL_SLUG))
        pid = cur.fetchone()[0]
        ids = {}
        for version, status, tag in (("2.0.0", "published", None),
                                     ("2.1.0-exp", "experimental", _SEAL_TAG)):
            cur.execute("""INSERT INTO artifacts (device_type, version, s3_path, checksum, is_active)
                           VALUES (%s, %s, %s, %s, true) RETURNING id""",
                        (_SEAL_SLUG, version, f"/data/content/binaries/{_SEAL_SLUG}/{version}.oxt",
                         f"sha256:{version}"))
            aid = cur.fetchone()[0]
            cur.execute("""INSERT INTO plugin_versions (plugin_id, version, artifact_id, status, tag, published_at)
                           VALUES (%s, %s, %s, %s, %s, NOW())""", (pid, version, aid, status, tag))
            ids[version] = aid
    try:
        yield pid
    finally:
        with conn.cursor() as cur:
            _purge(cur)
        conn.close()


def test_public_surfaces_never_expose_experimental_without_tag(sealed):
    """Le fail-safe de l'ADR-0004 tient par construction (`status='published'`
    partout) — mais rien ne l'attestait. Un `status IN (…)` ajouté par distraction
    passerait inaperçu. Ce test garde la décision d'architecture, pas le code.
    """
    mod = _load_main()

    liste = json.loads(bytes(mod.api_public_plugins().body))
    entree = next((p for p in liste["plugins"] if p["slug"] == _SEAL_SLUG), None)
    assert entree is not None, "le plugin de test doit apparaître au catalogue"
    assert entree["latest_version"] == "2.0.0", \
        f"la liste publique expose une version non publiée : {entree['latest_version']}"

    detail = json.loads(bytes(mod.api_public_plugin_detail(_SEAL_SLUG).body))
    assert detail["latest_version"] == "2.0.0"
    assert "experiments" not in detail, "la clé experiments ne doit exister qu'avec ?exp="

    redirection = mod.catalog_download(_SEAL_SLUG).headers.get("location", "")
    assert "2.0.0" in redirection and "2.1.0-exp" not in redirection, \
        f"/download sans tag a servi une branche : {redirection}"


def test_public_api_reveals_the_branch_only_with_its_tag(sealed):
    mod = _load_main()

    avec = json.loads(bytes(mod.api_public_plugin_detail(_SEAL_SLUG, exp=_SEAL_TAG).body))
    versions = [e["version"] for e in avec["experiments"]]
    assert versions == ["2.1.0-exp"], f"branche attendue, obtenu {versions}"
    assert avec["experiments"][0]["tag"] == _SEAL_TAG
    assert "2.1.0-exp" in avec["experiments"][0]["download_url"]
    assert avec["latest_version"] == "2.0.0", "la main reste la main, même sur la page taguée"

    faux = json.loads(bytes(mod.api_public_plugin_detail(_SEAL_SLUG, exp="mauvais-tag").body))
    assert faux["experiments"] == [], "un tag inconnu ne doit rien révéler"


def test_plugin_stats_counts_distinct_versions_across_the_fleet(fixt):
    """Le compteur doit venir de l'agrégat SQL : list_installations est paginée
    (20 lignes dans la route), un comptage sur la page serait faux dès le 21e
    appareil — et c'est précisément un parc nombreux qui fait cohabiter."""
    from app.admin.services import catalog as cat_svc
    cur, ids = fixt
    for i, version in enumerate(["1.5.0", "1.5.0", "1.6.0-rc1", "1.7.0-exp"]):
        cur.execute("""INSERT INTO plugin_installations (plugin_id, client_uuid, installed_version, status)
                       VALUES (%s, %s, %s, 'active')""",
                    (ids["plugin_id"], f"it-fleet-{i}", version))
    # Un appareil inactif ne compte pas : il ne « fait pas circuler » sa version.
    cur.execute("""INSERT INTO plugin_installations (plugin_id, client_uuid, installed_version, status)
                   VALUES (%s, 'it-fleet-off', '9.9.9', 'inactive')""", (ids["plugin_id"],))

    stats = cat_svc.get_plugin_stats(cur, ids["plugin_id"])
    assert stats["active"] == 4
    assert stats["distinct_versions"] == 3, \
        f"3 versions distinctes attendues sur le parc actif, obtenu {stats['distinct_versions']}"
