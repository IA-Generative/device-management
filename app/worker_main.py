from __future__ import annotations

import logging
import signal
import threading

from . import runtime_config

# Apply persisted overrides to os.environ + settings before importing .main /
# .settings (one-shot; also invoked by app.main at import). Makes restart-required
# overrides effective on boot for the worker too.
runtime_config.bootstrap_env_overrides()

from .main import _run_queue_worker_loop  # noqa: E402 (intentionnel : après bootstrap pré-import)
from .services.db import db_url_bootstrap  # noqa: E402
from .settings import settings  # noqa: E402

logger = logging.getLogger("device-management.worker")


def main() -> None:
    mode = str(settings.runtime_mode or "worker").strip().lower()
    if mode not in ("worker", "all"):
        logger.warning("Worker process disabled by DM_RUNTIME_MODE=%s", mode)
        return
    stop_event = threading.Event()

    def _handle_signal(_signum, _frame):
        stop_event.set()
        runtime_config.request_wake()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Worker pods do NOT run the FastAPI lifespan, so the config sync loop must be
    # started here too. Wait (bounded) for the first successful load before
    # claiming jobs, so we never process work with incomplete config.
    if db_url_bootstrap():
        runtime_config.start_background(stop_event, role="worker")
        if runtime_config.wait_until_ready(timeout=30):
            logger.info("Runtime config loaded; entering queue loop.")
        else:
            logger.warning("Runtime config not ready after 30s; entering queue loop anyway.")
    else:
        runtime_config.snapshot_baseline()
        logger.info("Runtime config sync disabled (no DB); worker uses ENV baseline.")

    _run_queue_worker_loop(stop_event=stop_event, once=False)


if __name__ == "__main__":
    main()
