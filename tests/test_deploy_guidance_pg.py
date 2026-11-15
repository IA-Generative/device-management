"""Intégration (vrai Postgres) — le déploiement d'une version guide l'administrateur
et signale ce qui ne sera servi à personne (issue #132).

Couvre les trois causes de l'issue et chaque ligne de son tableau :
  - version générale sans campagne : détectée, campagne par défaut créable ;
  - version suffixée « -rc.N » : servie par une campagne ordinaire ;
  - campagne qui ne sert personne : nombre de postes éligibles et raison ;
  - cibles incomplètes refusées, cible « pourcentage » réparée ;
  - double envoi d'un même numéro avec un autre binaire : refus ou confirmation ;
  - GET /api/campaigns.
Et, de bout en bout par les routes HTTP : envoi d'une version avec « deploy »,
puis /config d'un client en version antérieure contient un bloc `update`.

Marqué `integration`. Les tests de service tournent dans une transaction
annulée ; les tests de bout en bout valident réellement (les routes ouvrent
leurs propres connexions) et nettoient derrière eux.

Exécution : `DATABASE_URL=postgresql://dev:dev@localhost:5433/bootstrap \\
             pytest tests/test_deploy_guidance_pg.py -m integration`
"""
from __future__ import annotations

import io
import os
import sys
import types
import uuid
import zipfile

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "db", "schema.sql")

_SLUG = "it-deploy-guidance"
_DEVICE_TYPE = "it-deploy-guidance"
_TOKEN = "it-deploy-guidance-token"


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
    from app.services.db import apply_schema
    apply_schema(url, _SCHEMA_PATH)
    return url


@pytest.fixture(scope="module")
def mod(_db_url, tmp_path_factory):
    """app.main avec le VRAI psycopg2, binaires dans un dossier temporaire."""
    binaries = str(tmp_path_factory.mktemp("binaries"))
    os.environ["DATABASE_URL"] = _db_url
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DM_QUEUE_ADMIN_TOKEN"] = _TOKEN
    os.environ["DM_RUNTIME_MODE"] = "all"
    os.environ["DM_BINARIES_MODE"] = "local"
    os.environ["DM_LOCAL_BINARIES_DIR"] = binaries
    # Le transfert du binaire vers le pod admin n'a pas de destinataire ici.
    os.environ["DM_ADMIN_INTERNAL_URL"] = "http://127.0.0.1:9"
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    # Ne retirer psycopg2 que si un autre module de test y a laissé un faux :
    # ré-importer le vrai le priverait de ses sous-modules déjà chargés
    # (psycopg2.errors), dont dépendent d'autres tests d'intégration.
    current = sys.modules.get("psycopg2")
    if not (isinstance(current, types.ModuleType) and getattr(current, "__file__", None)):
        sys.modules.pop("psycopg2", None)
    import psycopg2  # le vrai module
    for name in [n for n in sys.modules if n == "app.main" or n == "app.settings"
                 or n.startswith("app.admin")]:
        sys.modules.pop(name, None)
    import app.main as m
    m.psycopg2 = psycopg2
    m._binaries_dir_for_tests = binaries
    return m


# ═════════════════════════════════════════════════════════════════════════════
# Services — dans une transaction annulée
# ═════════════════════════════════════════════════════════════════════════════

class _Fixt:
    def __init__(self, cur, plugin_id):
        self.cur = cur
        self.plugin_id = plugin_id

    def version(self, version: str, *, status: str = "published", checksum: str | None = None) -> tuple[int, int]:
        """(version_id, artifact_id)"""
        self.cur.execute("""
            INSERT INTO artifacts (device_type, version, s3_path, checksum, is_active)
            VALUES (%s, %s, %s, %s, true) RETURNING id
        """, (_DEVICE_TYPE, version, f"{_DEVICE_TYPE}/{_SLUG}-{version}.xpi",
              checksum or "sha256:" + "0" * 64))
        artifact_id = self.cur.fetchone()[0]
        self.cur.execute("""
            INSERT INTO plugin_versions (plugin_id, artifact_id, version, status,
                                         distribution_mode, published_at)
            VALUES (%s, %s, %s, %s, 'managed', now()) RETURNING id
        """, (self.plugin_id, artifact_id, version, status))
        return self.cur.fetchone()[0], artifact_id

    def device(self, version: str, *, email: str | None = None, client_uuid: str | None = None) -> str:
        client_uuid = client_uuid or str(uuid.uuid4())
        self.cur.execute("""
            INSERT INTO plugin_installations (plugin_id, client_uuid, email, installed_version, status)
            VALUES (%s, %s, %s, %s, 'active')
        """, (self.plugin_id, client_uuid, email, version))
        return client_uuid

    def campaign(self, artifact_id, **over) -> int:
        from app.admin.services import campaigns as svc
        kw = {"name": "IT 132", "status": "active", "plugin_id": self.plugin_id,
              "artifact_id": artifact_id, "target_cohort_id": None}
        kw.update(over)
        return svc.create_campaign(self.cur, **kw)

    def general(self, version_id) -> None:
        self.cur.execute("UPDATE plugins SET general_version_id = %s WHERE id = %s",
                         (version_id, self.plugin_id))


@pytest.fixture
def fixt(_db_url):
    import psycopg2
    conn = psycopg2.connect(_db_url)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO plugins (slug, name, device_type, status)
        VALUES (%s, 'IT guidage déploiement', %s, 'active') RETURNING id
    """, (_SLUG + "-svc", _DEVICE_TYPE))
    try:
        yield _Fixt(cur, cur.fetchone()[0])
    finally:
        conn.rollback()
        cur.close()
        conn.close()


def _svc():
    from app.admin.services import artifacts, campaigns, cohorts
    return campaigns, cohorts, artifacts


# ── Cause 1 : version générale sans campagne ─────────────────────────────────

def test_no_general_version_means_no_warning(fixt):
    campaigns, _, _ = _svc()
    fixt.version("1.0.0")
    assert campaigns.general_version_coverage(fixt.cur, fixt.plugin_id) is None


def test_general_version_without_campaign_is_reported(fixt):
    campaigns, _, _ = _svc()
    vid, _aid = fixt.version("1.0.0")
    fixt.general(vid)
    cov = campaigns.general_version_coverage(fixt.cur, fixt.plugin_id)
    assert cov == {"version_id": vid, "version": "1.0.0", "coverage": "none", "can_create": True}


def test_campaign_on_another_version_does_not_carry_the_general(fixt):
    campaigns, _, _ = _svc()
    vid, _ = fixt.version("1.0.0")
    _, other_artifact = fixt.version("0.9.0")
    fixt.general(vid)
    fixt.campaign(other_artifact)
    assert campaigns.general_version_coverage(fixt.cur, fixt.plugin_id)["coverage"] == "none"


def test_paused_or_draft_campaign_does_not_carry_the_general(fixt):
    campaigns, _, _ = _svc()
    vid, aid = fixt.version("1.0.0")
    fixt.general(vid)
    fixt.campaign(aid, status="draft")
    fixt.campaign(aid, status="paused")
    assert campaigns.general_version_coverage(fixt.cur, fixt.plugin_id)["coverage"] == "none"


def test_cohort_only_campaign_is_partial_coverage(fixt):
    campaigns, cohorts, _ = _svc()
    vid, aid = fixt.version("1.0.0")
    fixt.general(vid)
    cohort = cohorts.create_cohort(fixt.cur, name="IT 132 partiel", description="", type="manual")
    fixt.campaign(aid, target_cohort_id=cohort)
    assert campaigns.general_version_coverage(fixt.cur, fixt.plugin_id)["coverage"] == "partial"


def test_default_campaign_carries_the_general_to_the_whole_fleet(fixt):
    campaigns, _, _ = _svc()
    vid, aid = fixt.version("1.0.0")
    fixt.general(vid)
    cid = campaigns.create_default_campaign(fixt.cur, plugin_id=fixt.plugin_id, version_id=vid,
                                            created_by="it@test")
    fixt.cur.execute("""SELECT status, type, target_cohort_id, artifact_id, version_id, plugin_id,
                               rollout_config, is_experiment FROM campaigns WHERE id = %s""", (cid,))
    assert fixt.cur.fetchone() == ("active", "plugin_update", None, aid, vid, fixt.plugin_id, None, False)
    assert campaigns.general_version_coverage(fixt.cur, fixt.plugin_id)["coverage"] == "full"


def test_default_campaign_supersedes_the_previous_ordinary_one_only(fixt):
    campaigns, cohorts, _ = _svc()
    _, old_artifact = fixt.version("0.9.0")
    _, exp_artifact = fixt.version("1.1.0-exp")
    vid, _ = fixt.version("1.0.0")
    cohort = cohorts.create_cohort(fixt.cur, name="IT 132 expé", description="", type="manual")
    old = fixt.campaign(old_artifact)
    exp = fixt.campaign(exp_artifact, is_experiment=True, target_cohort_id=cohort)
    campaigns.create_default_campaign(fixt.cur, plugin_id=fixt.plugin_id, version_id=vid)
    fixt.cur.execute("SELECT id, status FROM campaigns WHERE id IN (%s, %s)", (old, exp))
    assert dict(fixt.cur.fetchall()) == {old: "completed", exp: "active"}


@pytest.mark.parametrize("status", ["draft", "yanked", "experimental"])
def test_default_campaign_refuses_a_version_that_cannot_go_to_everyone(fixt, status):
    campaigns, _, _ = _svc()
    vid, _ = fixt.version("1.0.0", status=status)
    with pytest.raises(campaigns.DefaultCampaignError):
        campaigns.create_default_campaign(fixt.cur, plugin_id=fixt.plugin_id, version_id=vid)


def test_default_campaign_refuses_an_unrecognised_version(fixt):
    campaigns, _, _ = _svc()
    vid, _ = fixt.version("v1.0")
    with pytest.raises(campaigns.DefaultCampaignError, match="Forme de version non reconnue"):
        campaigns.create_default_campaign(fixt.cur, plugin_id=fixt.plugin_id, version_id=vid)


def test_reach_flags_an_ordinary_campaign_on_an_unrecognised_version(fixt):
    campaigns, _, _ = _svc()
    _, aid = fixt.version("v1.0")
    fixt.device("0.9.0")
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid))
    assert reach["eligible"] == 0
    assert any("Forme de version non reconnue" in n for n in reach["notes"])
    pinned = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid, is_experiment=True))
    assert pinned["eligible"] == 1 and not any("non reconnue" in n for n in pinned["notes"])


def test_default_campaign_refuses_a_version_without_hosted_binary(fixt):
    campaigns, _, _ = _svc()
    fixt.cur.execute("""
        INSERT INTO plugin_versions (plugin_id, version, status, distribution_mode, download_url, published_at)
        VALUES (%s, '1.0.0', 'published', 'download_link', 'https://example.invalid/p.xpi', now())
        RETURNING id
    """, (fixt.plugin_id,))
    vid = fixt.cur.fetchone()[0]
    fixt.general(vid)
    assert campaigns.general_version_coverage(fixt.cur, fixt.plugin_id)["can_create"] is False
    with pytest.raises(campaigns.DefaultCampaignError):
        campaigns.create_default_campaign(fixt.cur, plugin_id=fixt.plugin_id, version_id=vid)


# ── Cause 3 : postes éligibles et raison du zéro ─────────────────────────────

def test_reach_counts_who_would_be_served_today(fixt):
    campaigns, _, _ = _svc()
    _, aid = fixt.version("1.0.0")
    for v in ("0.8.0", "0.9.0", "0.9.5"):
        fixt.device(v)
    fixt.device("1.0.0")
    fixt.device("1.1.0")
    fixt.device("")
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid))
    assert (reach["installed"], reach["eligible"]) == (6, 3)
    assert reach["reasons"] == {"up_to_date": 1, "ahead": 1, "no_version": 1}
    assert reach["zero_reason"] is None
    assert reach["target_version"] == "1.0.0"


def test_reach_matches_what_config_serves_device_by_device(fixt, mod):
    """Le compte de l'admin et /config appliquent la même règle."""
    campaigns, _, _ = _svc()
    _, aid = fixt.version("0.24.0-rc.179")
    versions = ["0.23.0", "0.24.0-rc.100", "0.24.0-rc.179", "0.24.0", "0.25.0", "0.24.0-rc.180"]
    devices = {fixt.device(v): v for v in versions}
    cid = fixt.campaign(aid)
    reach = campaigns.campaign_reach(fixt.cur, cid)
    camp = mod._resolve_active_campaign(fixt.cur, device_cohort_ids=[], device_type=_DEVICE_TYPE,
                                        platform_version="", plugin_id=fixt.plugin_id)
    assert camp["campaign_id"] == cid
    served = [v for u, v in devices.items()
              if mod._build_update_directive(plugin_version=v, campaign=camp, client_uuid=u,
                                             device_name=_SLUG)]
    assert sorted(served) == ["0.23.0", "0.24.0-rc.100"]
    assert reach["eligible"] == len(served)
    assert reach["reasons"] == {"up_to_date": 1, "ahead": 3}


def test_reach_zero_says_why_when_everyone_is_up_to_date(fixt):
    campaigns, _, _ = _svc()
    _, aid = fixt.version("1.0.0")
    fixt.device("1.0.0")
    fixt.device("1.2.0")
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid))
    assert reach["eligible"] == 0
    assert "1 déjà sur la version cible" in reach["zero_reason"]
    assert "1 sur une version postérieure" in reach["zero_reason"]


def test_reach_zero_when_the_cohort_holds_no_device_of_this_plugin(fixt):
    campaigns, cohorts, _ = _svc()
    _, aid = fixt.version("1.0.0")
    fixt.device("0.9.0", email="alice@example.org")
    cohort = cohorts.create_cohort(fixt.cur, name="IT 132 ailleurs", description="", type="manual")
    cohorts.add_members(fixt.cur, cohort, [("email", "bob@example.org")])
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid, target_cohort_id=cohort))
    assert reach["eligible"] == 0
    assert reach["reasons"] == {"outside_cohort": 1}
    assert "ne contient aucun poste de ce plugin" in reach["zero_reason"]
    assert "IT 132 ailleurs" in reach["zero_reason"]


def test_reach_manual_cohort_matches_email_or_uuid(fixt):
    campaigns, cohorts, _ = _svc()
    _, aid = fixt.version("1.0.0")
    fixt.device("0.9.0", email="alice@example.org")
    by_uuid = fixt.device("0.9.0")
    fixt.device("0.9.0", email="carol@example.org")
    cohort = cohorts.create_cohort(fixt.cur, name="IT 132 manuel", description="", type="manual")
    cohorts.add_members(fixt.cur, cohort, [("email", "alice@example.org"), ("client_uuid", by_uuid)])
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid, target_cohort_id=cohort))
    assert (reach["eligible"], reach["reasons"]) == (2, {"outside_cohort": 1})


def test_reach_flags_a_percentage_cohort_without_percentage(fixt):
    """Les cohortes créées avant le correctif (config vide) restent en base."""
    campaigns, cohorts, _ = _svc()
    _, aid = fixt.version("1.0.0")
    for _ in range(5):
        fixt.device("0.9.0")
    broken = cohorts.create_cohort(fixt.cur, name="IT 132 cassée", description="", type="percentage")
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid, target_cohort_id=broken))
    assert reach["eligible"] == 0
    assert any("n'a pas de pourcentage" in n for n in reach["notes"])


def test_reach_counts_the_current_rollout_stage(fixt):
    campaigns, _, _ = _svc()
    from app.services import update_rules
    _, aid = fixt.version("1.0.0")
    uuids = [fixt.device("0.9.0") for _ in range(200)]
    stages = {"stages": [{"percent": 25, "duration_hours": 24}, {"percent": 100, "duration_hours": 0}]}
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid, rollout_config=stages))
    expected = sum(update_rules.rollout_admits(u, 25) for u in uuids)
    assert reach["rollout_percent"] == 25
    assert reach["eligible"] == expected
    assert reach["reasons"] == {"rollout_waiting": 200 - expected}


def test_reach_of_an_inactive_campaign_says_nothing_is_served(fixt):
    campaigns, _, _ = _svc()
    _, aid = fixt.version("1.0.0")
    fixt.device("0.9.0")
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(aid, status="draft"))
    assert reach["eligible"] == 1, "ce qu'elle servirait une fois active"
    assert any("rien n'est servi" in n for n in reach["notes"])


def test_reach_of_a_campaign_without_artifact(fixt):
    campaigns, _, _ = _svc()
    fixt.device("0.9.0")
    reach = campaigns.campaign_reach(fixt.cur, fixt.campaign(None))
    assert reach["eligible"] == 0
    assert "pas d'artefact" in reach["zero_reason"]
    assert campaigns.campaign_reach(fixt.cur, -1) is None


# ── Cibles ───────────────────────────────────────────────────────────────────

def _target(fixt, **kw):
    _, cohorts, _ = _svc()
    base = {"target_mode": "all", "cohort_id": "", "percent": "10", "emails": "",
            "auto_name": f"auto-{_DEVICE_TYPE}-1.0.0-it"}
    base.update(kw)
    return cohorts.resolve_deploy_target(fixt.cur, **base)


def test_target_all_is_the_whole_fleet(fixt):
    assert _target(fixt) is None


def test_target_existing_without_cohort_is_refused_not_widened(fixt):
    _, cohorts, _ = _svc()
    with pytest.raises(cohorts.TargetError, match="choisissez une cohorte"):
        _target(fixt, target_mode="existing", cohort_id="")


def test_target_existing_unknown_cohort_is_refused(fixt):
    _, cohorts, _ = _svc()
    with pytest.raises(cohorts.TargetError, match="introuvable"):
        _target(fixt, target_mode="existing", cohort_id="2147483000")
    with pytest.raises(cohorts.TargetError):
        _target(fixt, target_mode="existing", cohort_id="abc")


def test_target_existing_returns_the_chosen_cohort(fixt):
    _, cohorts, _ = _svc()
    cid = cohorts.create_cohort(fixt.cur, name="IT 132 choisie", description="", type="manual")
    assert _target(fixt, target_mode="existing", cohort_id=str(cid)) == cid


@pytest.mark.parametrize("emails", ["", "   ", "\n\n  \n"])
def test_target_emails_empty_list_is_refused_not_widened(fixt, emails):
    _, cohorts, _ = _svc()
    with pytest.raises(cohorts.TargetError, match="liste est vide"):
        _target(fixt, target_mode="emails", emails=emails)


def test_target_emails_creates_a_manual_cohort_with_its_members(fixt):
    cid = _target(fixt, target_mode="emails", emails="alice@example.org\n\n bob@example.org \n")
    fixt.cur.execute("SELECT identifier_type, identifier_value FROM cohort_members "
                     "WHERE cohort_id = %s ORDER BY identifier_value", (cid,))
    assert fixt.cur.fetchall() == [("email", "alice@example.org"), ("email", "bob@example.org")]


def test_target_percent_cohort_carries_its_percentage_and_matches_devices(fixt, mod):
    """Le défaut : config vide → 0 % → la campagne ne servait jamais personne."""
    cid = _target(fixt, target_mode="percent", percent="40")
    fixt.cur.execute("SELECT type, config FROM cohorts WHERE id = %s", (cid,))
    assert fixt.cur.fetchone() == ("percentage", {"percentage": 40})
    uuids = [str(uuid.uuid4()) for _ in range(300)]
    inside = [u for u in uuids
              if cid in mod._resolve_device_cohorts(fixt.cur, email="", client_uuid=u)]
    assert 0.28 < len(inside) / len(uuids) < 0.52, "≈ 40 % du parc doit appartenir à la cohorte"


@pytest.mark.parametrize("percent", ["0", "101", "-5", "abc", ""])
def test_target_percent_out_of_range_is_refused(fixt, percent):
    _, cohorts, _ = _svc()
    with pytest.raises(cohorts.TargetError, match="entre 1 et 100"):
        _target(fixt, target_mode="percent", percent=percent)


# ── Double envoi ─────────────────────────────────────────────────────────────

def _overwrite(fixt, checksum, confirmed=False, version="1.0.0"):
    _, _, artifacts = _svc()
    return artifacts.check_overwrite(fixt.cur, device_type=_DEVICE_TYPE, platform_variant="",
                                     version=version, checksum=checksum, confirmed=confirmed)


def test_first_upload_of_a_version_passes(fixt):
    assert _overwrite(fixt, "sha256:" + "a" * 64) is False


def test_same_binary_again_passes_silently(fixt):
    fixt.version("1.0.0", checksum="sha256:" + "a" * 64)
    assert _overwrite(fixt, "sha256:" + "a" * 64) is False
    assert _overwrite(fixt, "A" * 64) is False, "même empreinte, écrite sans préfixe"


def test_different_binary_under_the_same_number_is_refused(fixt):
    _, _, artifacts = _svc()
    fixt.version("1.0.0", checksum="sha256:" + "a" * 64)
    with pytest.raises(artifacts.ArtifactConflict, match="existe déjà avec un binaire différent"):
        _overwrite(fixt, "sha256:" + "b" * 64)
    assert _overwrite(fixt, "sha256:" + "b" * 64, confirmed=True) is True


def test_other_version_or_unknown_checksum_does_not_conflict(fixt):
    fixt.version("1.0.0", checksum="sha256:" + "a" * 64)
    assert _overwrite(fixt, "sha256:" + "b" * 64, version="1.0.1") is False
    fixt.cur.execute("UPDATE artifacts SET checksum = NULL WHERE device_type = %s", (_DEVICE_TYPE,))
    assert _overwrite(fixt, "sha256:" + "b" * 64) is False, "sans empreinte connue, pas de divergence affirmée"


# ── Lecture par l'API ────────────────────────────────────────────────────────

def test_list_campaigns_api_gives_what_a_diagnosis_needs(fixt):
    campaigns, cohorts, _ = _svc()
    vid, aid = fixt.version("1.0.0")
    _, rc = fixt.version("1.1.0-rc.1")
    cohort = cohorts.create_cohort(fixt.cur, name="IT 132 liste", description="", type="manual")
    general = fixt.campaign(aid)
    fixt.cur.execute("UPDATE campaigns SET version_id = %s WHERE id = %s", (vid, general))
    arm = fixt.campaign(rc, is_experiment=True, target_cohort_id=cohort, priority=5)
    items = campaigns.list_campaigns_api(fixt.cur, plugin_slug=_SLUG + "-svc")
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {general, arm}
    g, a = by_id[general], by_id[arm]
    assert (g["plugin_slug"], g["version"], g["status"], g["is_experiment"]) == \
        (_SLUG + "-svc", "1.0.0", "active", False)
    assert g["target_cohort_id"] is None and isinstance(g["created_at"], str)
    assert (a["version"], a["is_experiment"], a["priority"], a["target_cohort_name"]) == \
        ("1.1.0-rc.1", True, 5, "IT 132 liste")
    assert campaigns.list_campaigns_api(fixt.cur, plugin_slug=_SLUG + "-svc", status="paused") == []
    assert campaigns.list_campaigns_api(fixt.cur, plugin_slug="it-132-inconnu") == []


# ═════════════════════════════════════════════════════════════════════════════
# De bout en bout — par les routes HTTP, données réellement validées
# ═════════════════════════════════════════════════════════════════════════════

def _xpi(marker: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("manifest.json", (2026, 1, 1, 0, 0, 0)), '{"name": "it"}')
        zf.writestr(zipfile.ZipInfo("marker.txt", (2026, 1, 1, 0, 0, 0)), marker)
    return buf.getvalue()


def _purge(db_url: str, slug: str, device_type: str) -> None:
    import psycopg2
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE plugins SET general_version_id = NULL WHERE slug = %s", (slug,))
        cur.execute("""DELETE FROM campaigns WHERE plugin_id IN (SELECT id FROM plugins WHERE slug = %s)
                       OR artifact_id IN (SELECT id FROM artifacts WHERE device_type = %s)""",
                    (slug, device_type))
        cur.execute("DELETE FROM plugin_versions WHERE plugin_id IN (SELECT id FROM plugins WHERE slug = %s)",
                    (slug,))
        cur.execute("DELETE FROM artifacts WHERE device_type = %s", (device_type,))
        cur.execute("DELETE FROM cohorts WHERE name LIKE %s", (f"auto-{device_type}-%",))
        cur.execute("DELETE FROM plugins WHERE slug = %s", (slug,))
    conn.close()


class _Site:
    """Un plugin réel en base et un client HTTP authentifié par le jeton de service."""

    def __init__(self, mod, db_url):
        from fastapi.testclient import TestClient
        self.mod = mod
        self.db_url = db_url
        # Adresse cliente réelle : le journal d'audit la range dans une colonne inet.
        self.http = TestClient(mod.app, client=("127.0.0.1", 50132))
        import psycopg2
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO plugins (slug, name, device_type, status)
                VALUES (%s, 'IT guidage e2e', %s, 'active') RETURNING id
            """, (_SLUG, _DEVICE_TYPE))
            self.plugin_id = cur.fetchone()[0]
        conn.close()

    def sql(self, query, params=()):
        import psycopg2
        conn = psycopg2.connect(self.db_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else None
        finally:
            conn.close()

    def upload(self, version: str, *, action: str = "deploy", marker: str | None = None, **fields):
        data = {"version": version, "action": action, "environment": "it",
                "deploy_strategy": "patch_all", "target_mode": "all"}
        data.update(fields)
        return self.http.post(
            f"/admin/catalog/{self.plugin_id}/versions/upload",
            data=data,
            files={"binary": (f"{_SLUG}-{version}.xpi", _xpi(marker or version), "application/octet-stream")},
            headers={"x-admin-token": _TOKEN}, follow_redirects=False)

    def config_update(self, plugin_version: str, client_uuid: str | None = None, email: str = ""):
        headers = {"X-Plugin-Version": plugin_version, "X-Platform-Type": "thunderbird",
                   "X-Platform-Version": "140.0", "X-Client-UUID": client_uuid or str(uuid.uuid4())}
        if email:
            headers["X-User-Email"] = email
        res = self.http.get(f"/config/{_SLUG}/config.json", headers=headers)
        assert res.status_code == 200, res.text
        return res.json()["update"]

    def versions(self) -> dict:
        return dict(self.sql("SELECT version, status FROM plugin_versions WHERE plugin_id = %s",
                             (self.plugin_id,)))

    def active_campaigns(self) -> list:
        return self.sql("""
            SELECT a.version, c.target_cohort_id, c.is_experiment
            FROM campaigns c JOIN artifacts a ON a.id = c.artifact_id
            WHERE c.plugin_id = %s AND c.status = 'active' ORDER BY c.id
        """, (self.plugin_id,))

    def stored_files(self) -> list:
        root = os.path.join(self.mod._binaries_dir_for_tests, _DEVICE_TYPE)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []


@pytest.fixture
def site(mod, _db_url):
    _purge(_db_url, _SLUG, _DEVICE_TYPE)
    s = _Site(mod, _db_url)
    try:
        yield s
    finally:
        _purge(_db_url, _SLUG, _DEVICE_TYPE)
        root = os.path.join(mod._binaries_dir_for_tests, _DEVICE_TYPE)
        if os.path.isdir(root):
            for name in os.listdir(root):
                os.remove(os.path.join(root, name))


def test_e2e_deploy_to_the_whole_fleet_then_config_serves_the_update(site):
    assert site.upload("1.0.0").status_code == 303
    assert site.active_campaigns() == [("1.0.0", None, False)]

    update = site.config_update("0.9.0")
    assert update is not None, "un client en version antérieure doit recevoir la directive"
    assert update["action"] == "update"
    assert update["target_version"] == "1.0.0"
    assert update["artifact_url"] == f"/catalog/{_SLUG}/download/{_SLUG}-1.0.0.xpi"
    assert update["checksum"].startswith("sha256:") and len(update["checksum"]) == 71
    assert update["plugin_slug"] == _SLUG

    assert site.config_update("1.0.0") is None, "déjà à jour"
    assert site.config_update("1.1.0") is None, "en avance, pas de rollback"

    # Le binaire annoncé est bien celui que la route de téléchargement sert.
    served = site.http.get(update["artifact_url"])
    assert served.status_code == 200
    import hashlib
    assert "sha256:" + hashlib.sha256(served.content).hexdigest() == update["checksum"]


def test_e2e_release_candidate_deployed_ordinarily_is_served(site):
    """Le cas de production : une « -rc.N » en campagne ordinaire ne servait personne."""
    assert site.upload("0.23.0").status_code == 303
    assert site.upload("0.24.0-rc.179").status_code == 303
    assert site.active_campaigns() == [("0.24.0-rc.179", None, False)]

    for installed in ("0.23.0", "0.24.0-rc.100"):
        update = site.config_update(installed)
        assert update is not None, f"client {installed} : la rc doit être servie"
        assert update["target_version"] == "0.24.0-rc.179"
        assert update["artifact_url"].endswith(f"{_SLUG}-0.24.0-rc.179.xpi")
    assert site.config_update("0.24.0-rc.179") is None

    # La finale remplace la rc ; un poste en finale ne redescend pas vers une rc.
    assert site.upload("0.24.0").status_code == 303
    assert site.config_update("0.24.0-rc.179")["target_version"] == "0.24.0"
    assert site.config_update("0.24.0") is None


def test_e2e_general_without_campaign_then_default_campaign(site):
    """« Publier sans déployer » + « générale » : rien pour /config, et le DM le sait."""
    assert site.upload("1.0.0", action="publish_only", make_general="1").status_code == 303
    assert site.active_campaigns() == []
    assert site.config_update("0.9.0") is None, "générale sans campagne : /config ne sert rien"

    import psycopg2

    from app.admin.services import campaigns as campaigns_svc
    conn = psycopg2.connect(site.db_url)
    with conn.cursor() as cur:
        assert campaigns_svc.general_version_coverage(cur, site.plugin_id)["coverage"] == "none"
    conn.close()

    res = site.http.post(f"/admin/catalog/{site.plugin_id}/general-campaign",
                         headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].endswith("?tab=deployments")
    assert site.active_campaigns() == [("1.0.0", None, False)]
    assert site.config_update("0.9.0")["target_version"] == "1.0.0"

    again = site.http.post(f"/admin/catalog/{site.plugin_id}/general-campaign",
                           headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert again.status_code == 400, "déjà portée : pas de seconde campagne"
    assert len(site.active_campaigns()) == 1


def test_e2e_default_campaign_needs_a_general_version(site):
    assert site.upload("1.0.0", action="publish_only").status_code == 303
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/general-campaign",
                         headers={"x-admin-token": _TOKEN}, follow_redirects=False)
    assert res.status_code == 400
    assert site.active_campaigns() == []


def test_e2e_experimental_action_creates_no_campaign(site):
    assert site.upload("1.0.0-proto", action="experimental", tag="it").status_code == 303
    assert site.versions() == {"1.0.0-proto": "experimental"}
    assert site.active_campaigns() == []


@pytest.mark.parametrize("fields, message", [
    ({"target_mode": "existing", "cohort_id": ""}, "choisissez une cohorte"),
    ({"target_mode": "emails", "emails": "  \n "}, "liste est vide"),
    ({"target_mode": "percent", "percent": "0"}, "entre 1 et 100"),
])
def test_e2e_incomplete_target_is_refused_and_leaves_nothing_behind(site, fields, message):
    res = site.upload("1.0.0", **fields)
    assert res.status_code == 400
    assert message in res.json()["detail"]
    assert site.versions() == {}, "refus : aucune version créée"
    assert site.active_campaigns() == [], "et surtout pas de campagne sur tout le parc"
    assert site.stored_files() == [], "ni de binaire orphelin sur disque"


@pytest.mark.parametrize("version", ["v1.0.0", "1.0.x", "nightly"])
def test_e2e_deploy_of_an_unrecognised_version_is_refused_with_the_way_out(site, version):
    """Une campagne ordinaire ne la servirait à personne : refus, pas de campagne muette."""
    assert site.upload("0.9.0").status_code == 303
    res = site.upload(version)
    assert res.status_code == 400
    assert "Forme de version non reconnue" in res.json()["detail"]
    assert "expérimentale" in res.json()["detail"]
    assert site.versions() == {"0.9.0": "published"}
    assert site.active_campaigns() == [("0.9.0", None, False)], "la campagne qui fonctionnait reste active"
    assert len(site.stored_files()) == 1

    api = site.http.post(f"/api/plugins/{_SLUG}/deploy", data={"version": version},
                         files={"binary": ("p.xpi", _xpi("x"), "application/octet-stream")},
                         headers={"X-Admin-Token": _TOKEN})
    assert api.status_code == 400 and api.json()["code"] == "version_format"

    # Publiée sans campagne, elle reste possible (pin d'expérimentation) et se voit.
    assert site.upload(version, action="experimental", tag="it").status_code == 303
    assert "forme non reconnue" in _page(site, f"/admin/catalog/{site.plugin_id}")


def test_e2e_four_number_fleet_receives_the_release_candidate(site):
    """Anciens postes en 0.23.0.NNN face à une rc 0.24.0 en campagne ordinaire."""
    assert site.upload("0.24.0-rc.179").status_code == 303
    for installed in ("0.23.0.100", "0.23.0.150"):
        assert site.config_update(installed)["target_version"] == "0.24.0-rc.179"
    assert site.config_update("0.24.0.195") is None, "X.Y.Z.N est postérieure à la rc de X.Y.Z"


def test_e2e_percent_target_serves_about_that_share(site):
    assert site.upload("1.0.0", target_mode="percent", percent="50").status_code == 303
    [(version, cohort_id, _)] = site.active_campaigns()
    assert version == "1.0.0" and cohort_id is not None
    assert site.sql("SELECT config FROM cohorts WHERE id = %s", (cohort_id,)) == [({"percentage": 50},)]
    served = sum(site.config_update("0.9.0") is not None for _ in range(120))
    assert 35 < served < 85, f"≈ 50 % attendu, {served}/120 servis (avant le correctif : 0)"


def test_e2e_emails_target_serves_only_those_addresses(site):
    assert site.upload("1.0.0", target_mode="emails", emails="alice@example.org").status_code == 303
    assert site.config_update("0.9.0", email="alice@example.org")["target_version"] == "1.0.0"
    assert site.config_update("0.9.0", email="bob@example.org") is None
    assert site.config_update("0.9.0") is None


def test_e2e_same_version_with_another_binary_needs_confirmation(site):
    assert site.upload("1.0.0", marker="premier").status_code == 303
    before = site.sql("SELECT checksum FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,))

    refused = site.upload("1.0.0", marker="second")
    assert refused.status_code == 409
    assert "existe déjà avec un binaire différent" in refused.json()["detail"]
    assert site.sql("SELECT checksum FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,)) == before
    served = site.http.get(f"/catalog/{_SLUG}/download/{_SLUG}-1.0.0.xpi")
    import hashlib
    assert "sha256:" + hashlib.sha256(served.content).hexdigest() == before[0][0], \
        "refus : le binaire sur disque n'a pas été écrasé"

    assert site.upload("1.0.0", marker="premier").status_code == 303, "même binaire : pas de conflit"
    assert site.upload("1.0.0", marker="second", confirm_overwrite="1").status_code == 303
    assert site.sql("SELECT checksum FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,)) != before


def test_e2e_api_deploy_refuses_another_binary_unless_overwrite(site):
    def deploy(marker, **fields):
        return site.http.post(
            f"/api/plugins/{_SLUG}/deploy",
            data={"version": "2.0.0", "strategy": "immediate", **fields},
            files={"binary": (f"{_SLUG}-2.0.0.xpi", _xpi(marker), "application/octet-stream")},
            headers={"X-Admin-Token": _TOKEN})

    assert deploy("premier").status_code == 201
    refused = deploy("second")
    assert refused.status_code == 409 and refused.json()["code"] == "version_exists"
    assert deploy("second", overwrite="true").status_code == 201
    assert deploy("x", cohort_id="abc").status_code == 400
    assert deploy("second", cohort_id="2147483000", overwrite="true").status_code == 400
    assert site.config_update("1.9.0")["target_version"] == "2.0.0"


def test_e2e_api_lists_campaigns_and_progress_carries_the_reach(site):
    assert site.http.get("/api/campaigns").status_code == 401
    assert site.upload("1.0.0").status_code == 303
    client_uuid = str(uuid.uuid4())
    assert site.config_update("0.9.0", client_uuid=client_uuid) is not None

    res = site.http.get(f"/api/campaigns?plugin={_SLUG}&status=active", headers={"X-Admin-Token": _TOKEN})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["count"] == 1
    item = body["campaigns"][0]
    assert (item["plugin_slug"], item["version"], item["status"], item["is_experiment"],
            item["target_cohort_id"]) == (_SLUG, "1.0.0", "active", False, None)

    progress = site.http.get(f"/api/campaigns/{item['id']}/progress",
                             headers={"X-Admin-Token": _TOKEN}).json()
    assert progress["notified"] == 1
    assert progress["reach"]["installed"] == 1 and progress["reach"]["eligible"] == 1


# ── Ce que l'administrateur voit ─────────────────────────────────────────────

def _page(site, path: str) -> str:
    import time

    from app.admin.auth import SESSION_COOKIE, _sign_session
    site.http.cookies.set(SESSION_COOKIE, _sign_session({
        "sub": "it-132", "email": "admin@test.invalid", "name": "IT 132",
        "exp": int(time.time()) + 600}))
    res = site.http.get(path)
    assert res.status_code == 200, res.text[:400]
    return res.text


def test_e2e_plugin_page_warns_until_the_general_version_has_a_campaign(site):
    assert site.upload("1.0.0", action="publish_only", make_general="1").status_code == 303
    html = _page(site, f"/admin/catalog/{site.plugin_id}")
    assert 'id="general-coverage-warning"' in html
    assert "La version generale 1.0.0 n'est portee par aucune campagne active" in html \
        or "La version generale 1.0.0 n&#39;est portee par aucune campagne active" in html
    assert f'action="/admin/catalog/{site.plugin_id}/general-campaign"' in html
    assert "Creer la campagne par defaut (tout le parc)" in html

    assert site.http.post(f"/admin/catalog/{site.plugin_id}/general-campaign",
                          headers={"x-admin-token": _TOKEN}, follow_redirects=False).status_code == 303
    assert 'id="general-coverage-warning"' not in _page(site, f"/admin/catalog/{site.plugin_id}")


def test_e2e_plugin_page_has_no_warning_without_general_version(site):
    assert site.upload("1.0.0", action="publish_only").status_code == 303
    assert 'id="general-coverage-warning"' not in _page(site, f"/admin/catalog/{site.plugin_id}")


def test_e2e_admin_shows_a_campaign_that_serves_nobody(site):
    """« Actif, 0/0 » ne disait rien : le zéro et sa raison doivent se lire."""
    assert site.upload("1.0.0").status_code == 303
    assert site.config_update("1.0.0") is None          # un poste, déjà à jour
    [(campaign_id,)] = site.sql("SELECT id FROM campaigns WHERE plugin_id = %s AND status = 'active'",
                                (site.plugin_id,))

    detail = _page(site, f"/admin/campaigns/{campaign_id}")
    assert 'id="campaign-reach"' in detail
    assert "0 poste(s) eligible(s) aujourd'hui sur 1 installation(s) active(s)" in detail \
        or "0 poste(s) eligible(s) aujourd&#39;hui sur 1 installation(s) active(s)" in detail
    assert "Cette campagne ne sert personne." in detail
    assert "déjà sur la version cible" in detail

    listing = _page(site, f"/admin/catalog/{site.plugin_id}?tab=deployments")
    assert ">0/1</a>" in listing
    assert "Ne sert personne." in listing

    assert site.config_update("0.9.0") is not None      # un second poste, en retard
    detail = _page(site, f"/admin/campaigns/{campaign_id}")
    assert "1 poste(s) eligible(s)" in detail and "sur 2 installation(s) active(s)" in detail
    assert "Cette campagne ne sert personne." not in detail


def test_e2e_legacy_deploy_form_applies_the_same_guards(site):
    """/admin/deploy/create (assistant de déploiement) : mêmes cibles, même garde de double envoi."""
    _page(site, "/admin/campaigns")     # pose la session d'administration

    def create(marker="a", **fields):
        data = {"device_type": _DEVICE_TYPE, "version": "3.0.0", "deploy_type": "immediate",
                "target_mode": "all", "name": _SLUG}
        data.update(fields)
        return site.http.post("/admin/deploy/create", data=data, follow_redirects=False,
                              files={"binary": (f"{_SLUG}-3.0.0.xpi", _xpi(marker), "application/octet-stream")})

    refused = create(target_mode="existing", cohort_id="")
    assert refused.status_code == 400 and "choisissez une cohorte" in refused.json()["detail"]
    assert create(target_mode="emails", emails="").status_code == 400
    assert site.stored_files() == [] and site.active_campaigns() == []

    assert create(target_mode="percent", percent="30").status_code == 303
    [(version, cohort_id, _)] = site.active_campaigns()
    assert version == "3.0.0"
    assert site.sql("SELECT config FROM cohorts WHERE id = %s", (cohort_id,)) == [({"percentage": 30},)]

    assert create(marker="b").status_code == 409
    assert create(marker="b", confirm_overwrite="1").status_code == 303


# ═════════════════════════════════════════════════════════════════════════════
# Tout se fait à l'envoi (complément de l'issue #132)
# ═════════════════════════════════════════════════════════════════════════════

# ── Cible d'une campagne d'expérimentation ───────────────────────────────────

def _validate(fixt, **kw):
    _, cohorts, _ = _svc()
    base = {"target_mode": "all", "cohort_id": "", "percent": "10", "emails": ""}
    base.update(kw)
    return cohorts.validate_deploy_target(fixt.cur, **base)


def test_validate_target_creates_nothing(fixt):
    fixt.cur.execute("SELECT count(*) FROM cohorts")
    before = fixt.cur.fetchone()[0]
    assert _validate(fixt) == ("all",)
    assert _validate(fixt, target_mode="percent", percent="40") == ("percent", 40)
    assert _validate(fixt, target_mode="emails", emails="a@example.org\nb@example.org") == \
        ("emails", ["a@example.org", "b@example.org"])
    fixt.cur.execute("SELECT count(*) FROM cohorts")
    assert fixt.cur.fetchone()[0] == before


def test_experiment_needs_a_bounded_target(fixt):
    _, cohorts, _ = _svc()
    with pytest.raises(cohorts.TargetError, match="pas tout le parc"):
        _validate(fixt, experiment=True)
    with pytest.raises(cohorts.TargetError, match="pourcentage"):
        _validate(fixt, experiment=True, target_mode="percent", percent="10")
    pct = cohorts.create_cohort(fixt.cur, name="IT 132 pct", description="", type="percentage",
                                config={"percentage": 10})
    with pytest.raises(cohorts.TargetError, match="cohorte en pourcentage"):
        _validate(fixt, experiment=True, target_mode="existing", cohort_id=str(pct))
    manual = cohorts.create_cohort(fixt.cur, name="IT 132 testeurs", description="", type="manual")
    assert _validate(fixt, experiment=True, target_mode="existing", cohort_id=str(manual)) == ("cohort", manual)
    assert _validate(fixt, experiment=True, target_mode="emails", emails="a@example.org") == \
        ("emails", ["a@example.org"])


# ── Aperçu avant de valider ──────────────────────────────────────────────────

def test_preview_says_how_many_devices_would_be_offered_the_version(fixt):
    campaigns, _, _ = _svc()
    _, old = fixt.version("0.9.0")
    fixt.campaign(old)
    for v in ("0.8.0", "0.9.0", "0.9.0", "1.0.0"):
        fixt.device(v)
    p = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.0.0", target=("all",))
    assert (p["eligible"], p["installed"]) == (3, 4)
    assert p["summary"] == "Proposée aujourd'hui à 3 poste(s) sur 4 installation(s) active(s)."
    assert p["reasons"] == {"up_to_date": 1}
    assert p["supersedes"] == ["0.9.0"], "la campagne ordinaire en cours sera clôturée : le dire"


def test_preview_says_nobody_and_why(fixt):
    campaigns, _, _ = _svc()
    fixt.device("1.0.0")
    fixt.device("1.2.0")
    p = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.0.0", target=("all",))
    assert p["eligible"] == 0
    assert p["summary"].startswith("Proposée à aucun poste. Aucun poste ne recevrait la version 1.0.0")
    assert "déjà sur la version cible" in p["summary"] and "version postérieure" in p["summary"]

    fixt.device("0.9.0", email="alice@example.org")
    p = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.0.0",
                                     target=("emails", ["bob@example.org"]))
    assert p["summary"] == "Proposée à aucun poste. La cohorte « 1 adresse(s) » ne contient aucun poste de ce plugin."


def test_preview_without_any_installation(fixt):
    campaigns, _, _ = _svc()
    p = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.0.0", target=("all",))
    assert p["summary"] == "Proposée à aucun poste. Aucune installation active connue pour ce plugin."


def test_preview_of_an_experiment_pins_and_supersedes_nothing(fixt):
    campaigns, _, _ = _svc()
    _, old = fixt.version("1.0.0")
    fixt.campaign(old)
    fixt.device("1.2.0", email="alice@example.org")
    fixt.device("0.9.0", email="bob@example.org")
    p = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.1.0-rc.1",
                                     target=("emails", ["alice@example.org"]), is_experiment=True)
    assert p["eligible"] == 1, "version épinglée : servie même à un poste plus récent"
    assert p["reasons"] == {"outside_cohort": 1}
    assert p["supersedes"] == []


def test_preview_first_stage_and_percent_target(fixt):
    campaigns, _, _ = _svc()
    from app.services import update_rules
    uuids = [fixt.device("0.9.0") for _ in range(200)]
    staged = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.0.0",
                                          target=("all",), first_stage_percent=5)
    assert staged["eligible"] == sum(update_rules.rollout_admits(u, 5) for u in uuids)
    assert "Premier palier : 5 %" in staged["summary"] or staged["eligible"] == 0
    share = campaigns.preview_deployment(fixt.cur, plugin_id=fixt.plugin_id, version="1.0.0",
                                         target=("percent", 30))
    assert share["eligible"] == sum(update_rules.percentage_cohort_admits(u, 30) for u in uuids)


# ── Rattachement au plugin des campagnes créées par « Nouvelle campagne » ────

def test_plugin_of_an_artifact_is_the_one_of_its_version(fixt):
    campaigns, _, _ = _svc()
    _, aid = fixt.version("1.0.0")
    assert campaigns.plugin_id_for_artifact(fixt.cur, aid) == fixt.plugin_id
    assert campaigns.plugin_id_for_artifact(fixt.cur, None) is None


def test_plugin_of_an_orphan_artifact_falls_back_on_the_device_type_if_unambiguous(fixt):
    campaigns, _, _ = _svc()
    fixt.cur.execute("""INSERT INTO artifacts (device_type, version, s3_path, checksum)
                        VALUES (%s, '9.9.9', 'x/y.xpi', 'sha256:0') RETURNING id""", (_DEVICE_TYPE,))
    orphan = fixt.cur.fetchone()[0]
    assert campaigns.plugin_id_for_artifact(fixt.cur, orphan) == fixt.plugin_id
    fixt.cur.execute("""INSERT INTO plugins (slug, name, device_type, status)
                        VALUES (%s, 'IT jumeau', %s, 'active')""", (_SLUG + "-twin", _DEVICE_TYPE))
    assert campaigns.plugin_id_for_artifact(fixt.cur, orphan) is None, "deux plugins du même type : on ne devine pas"


# ── De bout en bout ──────────────────────────────────────────────────────────

def test_e2e_experiment_campaign_created_at_upload(site):
    """Le geste qu'il fallait faire en deux écrans : envoyer, puis /admin/campaigns/new."""
    assert site.upload("1.0.0").status_code == 303
    res = site.upload("1.1.0-rc.1", campaign_kind="experiment", target_mode="emails",
                      emails="alice@example.org", make_general="1")
    assert res.status_code == 303

    assert site.versions() == {"1.0.0": "published", "1.1.0-rc.1": "experimental"}, \
        "la version d'essai ne déprécie pas la stable et ne devient pas « dernière publiée »"
    campaigns = site.active_campaigns()
    assert ("1.0.0", None, False) in campaigns, "la campagne ordinaire continue"
    [(version, cohort_id, is_experiment)] = [c for c in campaigns if c[0] == "1.1.0-rc.1"]
    assert is_experiment is True and cohort_id is not None
    assert site.sql("SELECT general_version_id FROM plugins WHERE id = %s", (site.plugin_id,)) == [(None,)]

    alice = site.config_update("1.0.0", email="alice@example.org")
    assert alice["target_version"] == "1.1.0-rc.1"
    assert alice["artifact_url"] == f"/catalog/{_SLUG}/download/{_SLUG}-1.1.0-rc.1.xpi"
    assert site.http.get(alice["artifact_url"]).status_code == 200
    assert site.config_update("1.2.0", email="alice@example.org")["target_version"] == "1.1.0-rc.1", \
        "expérimentation : version exacte imposée"
    assert site.config_update("1.1.0-rc.1", email="alice@example.org") is None
    assert site.config_update("0.9.0", email="bob@example.org")["target_version"] == "1.0.0"
    assert site.config_update("1.0.0", email="bob@example.org") is None


@pytest.mark.parametrize("fields, message", [
    ({"target_mode": "all"}, "pas tout le parc"),
    ({"target_mode": "percent", "percent": "10"}, "pourcentage"),
    ({"target_mode": "emails", "emails": ""}, "liste est vide"),
])
def test_e2e_experiment_at_upload_needs_a_bounded_target(site, fields, message):
    res = site.upload("1.1.0-rc.1", campaign_kind="experiment", **fields)
    assert res.status_code == 400 and message in res.json()["detail"]
    assert site.versions() == {} and site.active_campaigns() == [] and site.stored_files() == []


def test_e2e_experiment_at_upload_accepts_an_unrecognised_label(site):
    assert site.upload("nightly", campaign_kind="experiment", target_mode="emails",
                       emails="alice@example.org").status_code == 303
    assert site.config_update("1.0.0", email="alice@example.org")["target_version"] == "nightly"
    assert site.upload("x", campaign_kind="autre").status_code == 400


def _preview(site, **fields):
    body = {"version": "1.0.0", "campaign_kind": "ordinary", "target_mode": "all",
            "deploy_strategy": "patch_all"}
    body.update(fields)
    res = site.http.post(f"/admin/catalog/{site.plugin_id}/deploy-preview", json=body,
                         headers={"x-admin-token": _TOKEN})
    assert res.status_code == 200, res.text
    return res.json()


def test_e2e_preview_announces_before_anything_is_created(site):
    assert site.upload("0.9.0").status_code == 303
    assert site.config_update("0.8.0") is not None          # un poste connu, en 0.8.0
    before = (site.versions(), site.active_campaigns(), site.sql("SELECT count(*) FROM cohorts"))

    ok = _preview(site)
    assert ok["ok"] is True
    assert ok["summary"] == "Proposée aujourd'hui à 1 poste(s) sur 1 installation(s) active(s)."
    assert ok["supersedes"] == ["0.9.0"]

    assert _preview(site, version="0.8.0")["summary"].startswith("Proposée à aucun poste.")
    assert _preview(site, target_mode="percent", percent="50")["ok"] is True
    assert _preview(site, target_mode="emails", emails="x@example.org")["eligible"] == 0

    for refused, message in [
        ({"target_mode": "existing", "cohort_id": ""}, "choisissez une cohorte"),
        ({"target_mode": "emails", "emails": ""}, "liste est vide"),
        ({"version": "v1.0"}, "Forme de version non reconnue"),
        ({"version": ""}, "version est requise"),
        ({"campaign_kind": "experiment"}, "pas tout le parc"),
    ]:
        out = _preview(site, **refused)
        assert out["ok"] is False and message in out["error"], refused

    assert _preview(site, version="v1.0", campaign_kind="experiment", target_mode="emails",
                    emails="x@example.org")["ok"] is True, "l'expérimentation épingle un libellé libre"
    assert (site.versions(), site.active_campaigns(), site.sql("SELECT count(*) FROM cohorts")) == before, \
        "l'aperçu ne crée rien"
    assert site.http.post("/admin/catalog/2147483000/deploy-preview", json={"version": "1"},
                          headers={"x-admin-token": _TOKEN}).status_code == 404


def test_e2e_campaign_form_attaches_the_campaign_to_its_plugin(site):
    """/admin/campaigns/new ne passait pas plugin_id : la campagne n'apparaissait pas chez le plugin."""
    assert site.upload("1.0.0", action="publish_only").status_code == 303
    [(artifact_id,)] = site.sql("SELECT id FROM artifacts WHERE device_type = %s", (_DEVICE_TYPE,))
    _page(site, "/admin/campaigns")     # pose la session d'administration
    res = site.http.post("/admin/campaigns", follow_redirects=False,
                         data={"name": "IT 132 formulaire", "artifact_id": str(artifact_id),
                               "start_status": "active"})
    assert res.status_code == 303
    assert site.sql("SELECT plugin_id, status FROM campaigns WHERE name = 'IT 132 formulaire'") == \
        [(site.plugin_id, "active")]
    assert site.config_update("0.9.0")["target_version"] == "1.0.0"
    assert "IT 132 formulaire" in _page(site, f"/admin/catalog/{site.plugin_id}?tab=deployments")


def test_e2e_upload_screen_offers_everything_and_the_menu_reaches_campaigns(site):
    html = _page(site, f"/admin/catalog/{site.plugin_id}")
    assert '<a href="/admin/campaigns" class="">Campagnes</a>' in html, "entrée de menu absente"
    assert 'name="campaign_kind" value="ordinary" checked' in html
    assert 'name="campaign_kind" value="experiment"' in html
    assert 'id="nv-preview"' in html and "/deploy-preview" in html
    assert "Publier comme experimentale" not in html
    assert "Publier en version d'essai (lien, sans campagne)" in html \
        or "Publier en version d&#39;essai (lien, sans campagne)" in html
    assert 'class="active">Campagnes</a>' in _page(site, "/admin/campaigns")
