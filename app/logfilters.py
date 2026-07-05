"""Filtres de logging pour réduire le bruit répétitif de l'access-log.

Deux sources noient l'access-log uvicorn de lignes sans intérêt :
  - les sondes Kubernetes (liveness/readiness/startup) et l'ingress tapent
    ``/livez`` / ``/healthz`` / ``/readyz`` plusieurs fois par seconde ;
  - l'UI admin poll en boucle certains endpoints (ex. la table « flotte /
    propagation » de /admin/debug → ``/admin/api/config/propagation`` toutes les 5 s).
Sans filtre, l'activité réelle (ex. actions admin d'un auditeur) devient illisible
— c'est exactement le trou d'observabilité relevé.

``HealthProbeFilter`` (``PROBE_PATHS`` + ``POLL_PATHS`` = ``FILTERED_PATHS``) :
  - SUPPRIME les lignes d'access-log dont le chemin est filtré (sonde ou polling) ;
  - les COMPTE par chemin et émet, au plus une fois par ``summary_interval``
    secondes, une ligne de synthèse sur un logger non filtré — pour prouver que
    le filtrage est actif (et garder un signal de vie), sans saturer.

Le filtre est attaché au logger ``uvicorn.access`` (cf. app/main.py).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

# Chemins de sonde dont l'access-log est filtré.
PROBE_PATHS = ("/livez", "/healthz", "/readyz")

# Endpoints à fort polling par l'UI admin : même nuisance que les sondes
# (plusieurs requêtes/minute en boucle), même traitement. Ex. la table
# « flotte / propagation » de /admin/debug interroge /admin/api/config/propagation
# toutes les 5 s → sans filtre, elle noie l'access-log des pods.
POLL_PATHS = ("/admin/api/config/propagation",)

# Ensemble complet des chemins dont l'access-log est filtré (sondes + polling).
FILTERED_PATHS = PROBE_PATHS + POLL_PATHS


class HealthProbeFilter(logging.Filter):
    """Drop health-probe access-log records, with a periodic filtered-count summary."""

    def __init__(
        self,
        summary_interval: float = 900.0,
        summary_logger: str = "device-management",
        time_func: Callable[[], float] = time.time,
        startup_grace: float = 60.0,
    ) -> None:
        super().__init__()
        self._counts: dict[str, int] = {}
        self._summary_interval = summary_interval
        self._summary_logger = logging.getLogger(summary_logger)
        self._time = time_func
        self._last_summary: float | None = None
        self._lock = threading.Lock()
        # Pendant les premières `startup_grace` secondes (≈ démarrage du process),
        # les sondes PASSENT (loguées individuellement → visibilité de l'init, dont
        # la transition 503→200 de /readyz) ; ensuite elles sont filtrées + comptées.
        self._startup_grace = startup_grace
        self._created = time_func()

    @staticmethod
    def _extract_path(record: logging.LogRecord) -> str | None:
        """Best-effort extraction of the request path from an access-log record.

        uvicorn.access logs with ``record.args`` =
        ``(client_addr, method, full_path, http_version, status_code)``.
        Fallback: parse the rendered message ``... "GET /path HTTP/1.1" 200``.
        """
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            return args[2]
        try:
            msg = record.getMessage()
        except Exception:
            return None
        # message contient typiquement: '... "GET /livez HTTP/1.1" 200 OK'
        start = msg.find('"')
        if start == -1:
            return None
        end = msg.find('"', start + 1)
        if end == -1:
            return None
        inside = msg[start + 1:end].split()
        # inside ~ ["GET", "/livez", "HTTP/1.1"]
        if len(inside) >= 2:
            return inside[1]
        return None

    @staticmethod
    def _match_filtered(path: str | None) -> str | None:
        if not path:
            return None
        for p in FILTERED_PATHS:
            if path == p or path.startswith(p + "?"):
                return p
        return None

    def _maybe_emit_summary(self, now: float) -> None:
        """Émet la synthèse si l'intervalle est écoulé. À appeler sous lock."""
        if self._last_summary is None:
            self._last_summary = now
            return
        if now - self._last_summary >= self._summary_interval and self._counts:
            summary = " ".join(f"{k}={v}" for k, v in sorted(self._counts.items()))
            elapsed = int(now - self._last_summary)
            # Logger distinct (non filtré) → la ligne passe toujours.
            self._summary_logger.info(
                "access-log filtré (%ds): %s", elapsed, summary
            )
            self._counts = {}
            self._last_summary = now

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 (logging API)
        hit = self._match_filtered(self._extract_path(record))
        now = self._time()
        with self._lock:
            in_grace = (now - self._created) < self._startup_grace
            if hit is not None and not in_grace:
                self._counts[hit] = self._counts.get(hit, 0) + 1
            self._maybe_emit_summary(now)
        # Trafic réel → gardé. Chemin filtré (sonde ou polling admin) : gardé
        # pendant la fenêtre de grâce (init), supprimé ensuite (compté pour le récap).
        if hit is None:
            return True
        return in_grace
