from __future__ import annotations

import logging
import signal
import threading

from .main import _run_queue_worker_loop
from .settings import settings


logger = logging.getLogger("device-management.worker")


def main() -> None:
    mode = str(settings.runtime_mode or "worker").strip().lower()
    if mode not in ("worker", "all"):
        logger.warning("Worker process disabled by DM_RUNTIME_MODE=%s", mode)
        return
    stop_event = threading.Event()

    def _handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _run_queue_worker_loop(stop_event=stop_event, once=False)


if __name__ == "__main__":
    main()
