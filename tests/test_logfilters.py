"""Tests du filtre de logs répétitifs (app/logfilters.py).

Couvre le trou d'observabilité : les sondes /livez, /healthz, /readyz — et le
polling admin /admin/api/config/propagation (toutes les 5 s) — saturaient
l'access-log, masquant l'activité réelle. Le filtre doit :
  - laisser passer les sondes pendant une fenêtre de grâce au démarrage (visibilité
    de l'init, dont la transition 503→200 de /readyz) ;
  - les supprimer ensuite, en émettant un récap périodique par chemin ;
  - laisser passer le trafic réel.
"""

import logging

from app.logfilters import FILTERED_PATHS, POLL_PATHS, PROBE_PATHS, HealthProbeFilter


def _access_record(path: str) -> logging.LogRecord:
    """Fabrique un LogRecord façon uvicorn.access (args = tuple à 5 éléments)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("100.64.0.1:5000", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_readyz_is_a_probe_path():
    assert "/readyz" in PROBE_PATHS and "/livez" in PROBE_PATHS and "/healthz" in PROBE_PATHS


def test_drops_probe_paths_after_grace():
    # startup_grace=0 → pas de fenêtre de grâce, comportement de filtrage direct.
    f = HealthProbeFilter(startup_grace=0)
    for p in PROBE_PATHS:
        assert f.filter(_access_record(p)) is False
    assert f.filter(_access_record("/readyz?x=1")) is False  # avec query string


def test_keeps_probes_during_startup_grace():
    clock = {"t": 100.0}
    f = HealthProbeFilter(startup_grace=60, time_func=lambda: clock["t"])
    # pendant la grâce (t < 100+60) → sondes GARDÉES (loguées), non comptées
    clock["t"] = 120
    assert f.filter(_access_record("/readyz")) is True
    assert f._counts == {}
    # après la grâce → supprimées + comptées
    clock["t"] = 200
    assert f.filter(_access_record("/readyz")) is False
    assert f._counts.get("/readyz") == 1


def test_config_propagation_is_a_poll_path():
    # L'endpoint pollé toutes les 5 s par /admin/debug est filtré comme les sondes.
    assert "/admin/api/config/propagation" in POLL_PATHS
    assert "/admin/api/config/propagation" in FILTERED_PATHS
    # les sondes restent dans l'ensemble filtré
    assert set(PROBE_PATHS) <= set(FILTERED_PATHS)


def test_drops_config_propagation_after_grace():
    f = HealthProbeFilter(startup_grace=0)
    assert f.filter(_access_record("/admin/api/config/propagation")) is False
    assert f._counts.get("/admin/api/config/propagation") == 1


def test_keeps_config_propagation_during_grace():
    clock = {"t": 100.0}
    f = HealthProbeFilter(startup_grace=60, time_func=lambda: clock["t"])
    clock["t"] = 120  # dans la grâce → gardé, non compté
    assert f.filter(_access_record("/admin/api/config/propagation")) is True
    assert f._counts == {}


def test_keeps_real_traffic():
    f = HealthProbeFilter(startup_grace=0)
    assert f.filter(_access_record("/admin/api/catalog/suggest")) is True
    assert f.filter(_access_record("/admin/devices/abc")) is True
    # /admin/api/config (à la demande) n'est PAS filtré — seul .../propagation l'est
    assert f.filter(_access_record("/admin/api/config")) is True


def test_extract_path_from_message_fallback():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            '1.2.3.4 - "GET /healthz HTTP/1.1" 200 OK', None, None)
    assert HealthProbeFilter._extract_path(rec) == "/healthz"


def test_periodic_summary_emitted(caplog):
    clock = {"t": 1000.0}
    f = HealthProbeFilter(summary_interval=900, time_func=lambda: clock["t"], startup_grace=0)

    f.filter(_access_record("/livez"))          # arme _last_summary
    for _ in range(4):
        f.filter(_access_record("/livez"))
    f.filter(_access_record("/healthz"))
    f.filter(_access_record("/readyz"))

    with caplog.at_level(logging.INFO, logger="device-management"):
        clock["t"] += 901  # dépasse l'intervalle (15 min)
        f.filter(_access_record("/livez"))       # déclenche le récap

    summaries = [r.getMessage() for r in caplog.records
                 if "access-log filtré" in r.getMessage()]
    assert summaries, "le récap périodique doit être émis"
    msg = summaries[-1]
    assert "/livez=" in msg and "/healthz=" in msg and "/readyz=" in msg


def test_summary_resets_counts(caplog):
    clock = {"t": 0.0}
    f = HealthProbeFilter(summary_interval=10, time_func=lambda: clock["t"], startup_grace=0)
    f.filter(_access_record("/livez"))   # arme
    clock["t"] = 11
    with caplog.at_level(logging.INFO, logger="device-management"):
        f.filter(_access_record("/livez"))  # émet récap n°1, reset
    assert f._counts == {}
