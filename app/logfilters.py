"""Filtres de logging pour réduire le bruit des sondes de santé.

Les sondes Kubernetes (liveness/readiness/startup) et l'ingress tapent
``/livez`` / ``/healthz`` plusieurs fois par seconde. Sans filtre, l'access-log
uvicorn est saturé par ces lignes et l'activité réelle (ex. actions admin d'un
auditeur) devient illisible — c'est exactement le trou d'observabilité relevé.

``HealthProbeFilter`` :
  - SUPPRIME les lignes d'access-log dont le chemin est une sonde de santé ;
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
PROBE_PATHS = ("/livez", "/healthz")


class HealthProbeFilter(logging.Filter):
    """Drop health-probe access-log records, with a periodic filtered-count summary."""

    def __init__(
        self,
        summary_interval: float = 3600.0,
        summary_logger: str = "device-management",
        time_func: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._counts: dict[str, int] = {}
        self._summary_interval = summary_interval
        self._summary_logger = logging.getLogger(summary_logger)
        self._time = time_func
        self._last_summary: float | None = None
        self._lock = threading.Lock()

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
    def _match_probe(path: str | None) -> str | None:
        if not path:
            return None
        for p in PROBE_PATHS:
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
                "health probes filtrés (%ds): %s", elapsed, summary
            )
            self._counts = {}
            self._last_summary = now

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 (logging API)
        probe = self._match_probe(self._extract_path(record))
        now = self._time()
        with self._lock:
            if probe is not None:
                self._counts[probe] = self._counts.get(probe, 0) + 1
            self._maybe_emit_summary(now)
        # True = on garde la ligne ; False = on la supprime.
        return probe is None
