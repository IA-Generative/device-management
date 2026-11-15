"""Règles de la directive de mise à jour, pré-versions comprises (issue #132).

Le constat de départ : une version « 0.24.0-rc.179 » rendue générale et portée
par une campagne ordinaire active n'était servie à personne, parce que
parse_version_tuple la réduisait à (0,) et que « installée < (0,) » est faux
pour tout poste. Les trois clients simulés de l'issue (0.23.0, 0.24.0-rc.100,
0.24.0-rc.179) recevaient tous `"update": null`.
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.services import update_rules as rules
from app.services.versions import parse_version_tuple, update_sort_key, version_sort_key

# ── Ordre des versions ───────────────────────────────────────────────────────

_ORDERED = [
    "0.23.0",
    "0.24.0-rc",
    "0.24.0-rc.1",
    "0.24.0-rc.100",
    "0.24.0-rc.179",
    "0.24.0-rc.1000",
    "0.24.0",
    "0.24.1-rc1",
    "0.24.1-rc2",
    "0.24.1-rc10",
    "0.24.1",
    "0.100.0",
    "1.0.0-alpha",
    "1.0.0-beta",
    "1.0.0",
]


def test_versions_sort_in_release_order():
    keys = [version_sort_key(v) for v in _ORDERED]
    assert None not in keys
    for earlier, later in zip(_ORDERED, _ORDERED[1:], strict=False):
        assert version_sort_key(earlier) < version_sort_key(later), f"{earlier} doit précéder {later}"


@pytest.mark.parametrize("version", ["1.2.3", "2.0", "24.8.1.2", "10"])
def test_plain_versions_compare_exactly_as_before(version):
    """Aucune régression sur les versions sans suffixe : même ordre que parse_version_tuple."""
    others = ["1.2.3", "1.2.10", "2.0", "2.0.0", "24.8.1.2", "9", "10"]
    for other in others:
        old = (parse_version_tuple(version) > parse_version_tuple(other)) - \
              (parse_version_tuple(version) < parse_version_tuple(other))
        new = (update_sort_key(version) > update_sort_key(other)) - \
              (update_sort_key(version) < update_sort_key(other))
        assert old == new, f"{version} vs {other}"


def test_build_metadata_is_ignored():
    assert version_sort_key("1.2.0+build.7") == version_sort_key("1.2.0")


@pytest.mark.parametrize("version", ["", None, "abc", "1.2.x", "1.2.0-", "1.2.0-rc..1", "v1.2.0"])
def test_unreadable_version_has_no_key_and_keeps_the_historic_fallback(version):
    assert version_sort_key(version) is None
    assert update_sort_key(version) == update_sort_key("0"), "repli historique : équivalent de (0,)"


def test_flag_gating_parser_is_untouched():
    """Le gating des flags s'appuie sur le repli (0,) pour une version suffixée."""
    assert parse_version_tuple("1.6.0-rc1") == (0,)


# ── decide_update ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("installed, target, expected", [
    # Les trois clients de l'issue face à la campagne ordinaire 0.24.0-rc.179.
    ("0.23.0", "0.24.0-rc.179", (rules.UPDATE, rules.UPDATE)),
    ("0.24.0-rc.100", "0.24.0-rc.179", (rules.UPDATE, rules.UPDATE)),
    ("0.24.0-rc.179", "0.24.0-rc.179", (None, rules.UP_TO_DATE)),
    # La rc est remplacée par la finale ; la finale ne redescend pas vers une rc.
    ("0.24.0-rc.179", "0.24.0", (rules.UPDATE, rules.UPDATE)),
    ("0.24.0", "0.24.0-rc.179", (None, rules.AHEAD)),
    ("0.24.0-rc.200", "0.24.0-rc.179", (None, rules.AHEAD)),
    # Versions ordinaires : comportement inchangé.
    ("1.5.0", "1.6.0", (rules.UPDATE, rules.UPDATE)),
    ("1.6.0", "1.6.0", (None, rules.UP_TO_DATE)),
    ("2.0.0", "1.6.0", (None, rules.AHEAD)),
    ("1.6.0", "1.6.0+build2", (None, rules.UP_TO_DATE)),
    # Version installée inconnue, cible absente.
    ("", "1.6.0", (None, rules.NO_VERSION)),
    ("unknown", "1.6.0", (None, rules.NO_VERSION)),
    ("0", "1.6.0", (None, rules.NO_VERSION)),
    ("1.5.0", "", (None, rules.NO_TARGET)),
])
def test_decide_update_ordinary_campaign(installed, target, expected):
    assert rules.decide_update(plugin_version=installed, target_version=target) == expected


@pytest.mark.parametrize("installed, target, served", [
    # Anciens postes numérotés sur quatre nombres (0.23.0.NNN) : lisibles, et
    # antérieurs à toute 0.24.0, rc comprise.
    ("0.23.0.100", "0.24.0-rc.179", True),
    ("0.23.0.150", "0.24.0", True),
    ("0.24.0-rc.179", "0.23.0.150", False),
    # Quatre nombres contre rc du même cœur : la rc précède X.Y.Z, donc X.Y.Z.N aussi.
    ("0.24.0-rc.179", "0.24.0.195", True),
    ("0.24.0.195", "0.24.0-rc.179", False),
    # Le quatrième nombre est comparé (comportement historique du DM, inchangé).
    ("0.23.0.100", "0.23.0.150", True),
    ("0.24.0.194", "0.24.0.195", True),
    ("0.24.0.195", "0.24.0.194", False),
    ("0.24.0", "0.24.0.195", True),
])
def test_four_number_versions_stay_readable(installed, target, served):
    assert version_sort_key(installed) is not None and version_sort_key(target) is not None
    action, _ = rules.decide_update(plugin_version=installed, target_version=target)
    assert (action == rules.UPDATE) is served


@pytest.mark.parametrize("target", ["v1.2.0", "1.2.x", "nightly", "1.2.0-"])
def test_unreadable_target_is_served_by_an_experiment_only(target):
    """Repli (0,) réservé à l'illisible : aucune campagne ordinaire ne la sert."""
    assert rules.decide_update(plugin_version="1.0.0", target_version=target)[0] is None
    assert rules.decide_update(plugin_version="1.0.0", target_version=target,
                               is_experiment=True)[0] == rules.UPDATE


def test_unorderable_version_message_names_the_version_and_the_way_out():
    from app.services.versions import unorderable_version_message
    for readable in ("1.2.3", "0.24.0-rc.179", "0.23.0.150", "1.0.0-proto", "2.0+build"):
        assert unorderable_version_message(readable) is None
    msg = unorderable_version_message("v1.2")
    assert "« v1.2 »" in msg and "1.2.3-rc.4" in msg and "expérimentation" in msg


def test_decide_update_rollback_only_when_configured():
    assert rules.decide_update(plugin_version="2.0.0", target_version="1.6.0",
                               has_rollback=True) == (rules.ROLLBACK, rules.ROLLBACK)
    assert rules.decide_update(plugin_version="2.0.0", target_version="1.6.0",
                               has_rollback=False) == (None, rules.AHEAD)


def test_decide_update_experiment_pins_whatever_the_order():
    for installed in ("1.5.0", "2.0.0", "1.6.0-rc2"):
        assert rules.decide_update(plugin_version=installed, target_version="1.6.0-rc1",
                                   is_experiment=True)[0] == rules.UPDATE
    assert rules.decide_update(plugin_version="1.6.0-rc1", target_version="1.6.0-rc1",
                               is_experiment=True) == (None, rules.UP_TO_DATE)


# ── Paliers et cohorte « pourcentage » ───────────────────────────────────────

def test_rollout_admits_is_stable_and_proportional():
    uuids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(2000)]
    assert all(rules.rollout_admits(u, 100) for u in uuids)
    assert not any(rules.rollout_admits(u, 0) for u in uuids)
    admitted = [u for u in uuids if rules.rollout_admits(u, 25)]
    assert 0.18 < len(admitted) / len(uuids) < 0.32
    assert admitted == [u for u in uuids if rules.rollout_admits(u, 25)], "tirage stable"
    assert rules.rollout_admits("", 5) is True, "sans UUID, /config ne peut pas tirer : admis"


def test_campaign_rollout_percent_follows_the_stages():
    stages = {"stages": [{"percent": 5, "duration_hours": 24},
                         {"percent": 25, "duration_hours": 24},
                         {"percent": 100, "duration_hours": 0}]}
    now = datetime.now(UTC)
    assert rules.campaign_rollout_percent(stages, now) == 5
    assert rules.campaign_rollout_percent(stages, now - timedelta(hours=30)) == 25
    assert rules.campaign_rollout_percent(stages, now - timedelta(hours=72)) == 100
    assert rules.campaign_rollout_percent(None, now) == 100
    assert rules.campaign_rollout_percent({"stages": []}, now) == 100


def test_percentage_cohort_with_zero_contains_nobody():
    """Le défaut d'origine : cohorte créée sans pourcentage → 0 → personne."""
    uuids = [f"u-{i}" for i in range(500)]
    assert not any(rules.percentage_cohort_admits(u, 0) for u in uuids)
    assert all(rules.percentage_cohort_admits(u, 100) for u in uuids)
    assert rules.percentage_cohort_admits("", 100) is False
    share = sum(rules.percentage_cohort_admits(u, 10) for u in uuids) / len(uuids)
    assert 0.04 < share < 0.17


# ── /config : la directive servie ────────────────────────────────────────────

@pytest.fixture(scope="module")
def mod():
    os.environ["DM_STORE_ENROLL_LOCALLY"] = "false"
    os.environ["DM_STORE_ENROLL_S3"] = "false"
    os.environ["DM_CONFIG_ENABLED"] = "true"
    os.environ["DM_AUTH_VERIFY_ACCESS_TOKEN"] = "false"
    os.environ["DATABASE_URL"] = "postgresql://fake:fake@localhost/fake"
    for name in ("app.main", "app.settings"):
        sys.modules.pop(name, None)
    return importlib.import_module("app.main")


def _campaign(version: str, **over) -> dict:
    base = {
        "campaign_id": 132, "urgency": "normal", "deadline_iso": None,
        "artifact_version": version,
        "artifact_s3_path": f"thunderbird/plugin-{version}.xpi",
        "artifact_checksum": "sha256:" + "a" * 64, "changelog_url": None,
        "rollback_s3_path": None, "rollback_version": "", "rollback_checksum": "",
        "rollout_config": None, "campaign_created_at": None, "is_experiment": False,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("installed", ["0.23.0", "0.24.0-rc.100"])
def test_ordinary_campaign_now_serves_a_release_candidate(mod, installed):
    d = mod._build_update_directive(plugin_version=installed,
                                    campaign=_campaign("0.24.0-rc.179"),
                                    client_uuid="u1", device_name="mirai-thunderbird")
    assert d is not None, "une campagne ordinaire sur une rc ne servait jamais personne"
    assert d["action"] == "update"
    assert d["target_version"] == "0.24.0-rc.179"
    assert d["artifact_url"].endswith("mirai-thunderbird-0.24.0-rc.179.xpi")
    assert d["checksum"].startswith("sha256:")


def test_client_already_on_the_release_candidate_gets_nothing(mod):
    assert mod._build_update_directive(plugin_version="0.24.0-rc.179",
                                       campaign=_campaign("0.24.0-rc.179"),
                                       client_uuid="u1", device_name="x") is None


def test_final_release_replaces_the_candidate_and_is_never_downgraded(mod):
    up = mod._build_update_directive(plugin_version="0.24.0-rc.179", campaign=_campaign("0.24.0"),
                                     client_uuid="u1", device_name="x")
    assert up is not None and up["target_version"] == "0.24.0"
    assert mod._build_update_directive(plugin_version="0.24.0", campaign=_campaign("0.24.0-rc.179"),
                                       client_uuid="u1", device_name="x") is None


def test_rollout_stage_still_gates_the_directive(mod):
    stages = {"stages": [{"percent": 5, "duration_hours": 24}, {"percent": 100, "duration_hours": 0}]}
    camp = _campaign("2.0.0", rollout_config=stages, campaign_created_at=datetime.now(UTC))
    uuids = [f"dev-{i}" for i in range(400)]
    served = [u for u in uuids
              if mod._build_update_directive(plugin_version="1.0.0", campaign=camp,
                                             client_uuid=u, device_name="x")]
    assert 0 < len(served) < 60, "palier 5 % : une petite part du parc seulement"
    assert served == [u for u in uuids if rules.rollout_admits(u, 5)]


def test_rollback_directive_unchanged(mod):
    camp = _campaign("1.6.0", rollback_s3_path="thunderbird/plugin-1.5.0.xpi",
                     rollback_version="1.5.0", rollback_checksum="sha256:" + "b" * 64)
    d = mod._build_update_directive(plugin_version="2.0.0", campaign=camp,
                                    client_uuid="u1", device_name="x")
    assert d["action"] == "rollback" and d["target_version"] == "1.5.0"
