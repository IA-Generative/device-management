"""Tests du filtre de logs des sondes de santé (app/logfilters.py).

Couvre le trou d'observabilité relevé en audit : les sondes /livez & /healthz
saturaient l'access-log du pod admin, masquant l'activité réelle. Le filtre doit
les supprimer, laisser passer le reste, et émettre un récap périodique.
"""

import logging

from app.logfilters import HealthProbeFilter, PROBE_PATHS


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


def test_drops_probe_paths():
    f = HealthProbeFilter()
    for p in PROBE_PATHS:
        assert f.filter(_access_record(p)) is False
    # avec query string aussi
    assert f.filter(_access_record("/livez?x=1")) is False


def test_keeps_real_traffic():
    f = HealthProbeFilter()
    assert f.filter(_access_record("/admin/api/catalog/suggest")) is True
    assert f.filter(_access_record("/admin/devices/abc")) is True


def test_extract_path_from_message_fallback():
    # args non exploitables → on retombe sur le parsing du message rendu
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            '1.2.3.4 - "GET /healthz HTTP/1.1" 200 OK', None, None)
    assert HealthProbeFilter._extract_path(rec) == "/healthz"


def test_periodic_summary_emitted(caplog):
    clock = {"t": 1000.0}
    f = HealthProbeFilter(summary_interval=3600, time_func=lambda: clock["t"])

    # 1er enregistrement → arme le compteur de fenêtre (pas de récap encore)
    f.filter(_access_record("/livez"))
    # quelques probes supplémentaires dans la fenêtre
    for _ in range(4):
        f.filter(_access_record("/livez"))
    f.filter(_access_record("/healthz"))

    with caplog.at_level(logging.INFO, logger="device-management"):
        clock["t"] += 3601  # dépasse l'intervalle
        f.filter(_access_record("/livez"))  # déclenche l'émission du récap

    summaries = [r.getMessage() for r in caplog.records
                 if "health probes filtrés" in r.getMessage()]
    assert summaries, "le récap horaire doit être émis"
    msg = summaries[-1]
    assert "/livez=" in msg and "/healthz=" in msg


def test_summary_resets_counts(caplog):
    clock = {"t": 0.0}
    f = HealthProbeFilter(summary_interval=10, time_func=lambda: clock["t"])
    f.filter(_access_record("/livez"))   # arme
    clock["t"] = 11
    with caplog.at_level(logging.INFO, logger="device-management"):
        f.filter(_access_record("/livez"))  # émet récap n°1, reset
    # après reset, les compteurs repartent de zéro
    assert f._counts == {}
