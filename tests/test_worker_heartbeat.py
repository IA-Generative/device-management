"""Cloud-native readiness — heartbeat de liveness du worker de queue
(DM_WORKER_HEARTBEAT_FILE) et arrêt propre entre deux jobs d'un même batch."""

import threading

import app.main as m
from app.postgres_queue import QueueJob


def test_heartbeat_file_touched_even_with_no_jobs(monkeypatch, tmp_path):
    heartbeat_path = tmp_path / "heartbeat"
    monkeypatch.setenv("DM_WORKER_HEARTBEAT_FILE", str(heartbeat_path))

    class _EmptyQueue:
        def claim_jobs(self, *, worker_id, limit):
            return []

    monkeypatch.setattr(m, "_get_queue_manager", lambda: _EmptyQueue())
    m._run_queue_worker_loop(stop_event=None, once=True)

    assert heartbeat_path.exists()


def test_heartbeat_updated_between_jobs(monkeypatch, tmp_path):
    heartbeat_path = tmp_path / "heartbeat"
    monkeypatch.setenv("DM_WORKER_HEARTBEAT_FILE", str(heartbeat_path))

    jobs = [
        QueueJob(id="1", topic="t", payload={}, attempts=0, max_attempts=3, dedupe_key=None),
        QueueJob(id="2", topic="t", payload={}, attempts=0, max_attempts=3, dedupe_key=None),
    ]

    class _OneBatchQueue:
        def __init__(self):
            self.served = False
            self.acked = []

        def claim_jobs(self, *, worker_id, limit):
            if self.served:
                return []
            self.served = True
            return jobs

        def ack(self, *, job_id, worker_id):
            self.acked.append(job_id)

    queue = _OneBatchQueue()
    monkeypatch.setattr(m, "_get_queue_manager", lambda: queue)
    monkeypatch.setattr(m, "_process_queue_job", lambda job: None)

    heartbeat_calls = {"n": 0}
    original_touch = m._touch_worker_heartbeat

    def _counting_touch():
        heartbeat_calls["n"] += 1
        original_touch()

    monkeypatch.setattr(m, "_touch_worker_heartbeat", _counting_touch)

    m._run_queue_worker_loop(stop_event=None, once=True)

    assert queue.acked == ["1", "2"]
    # One touch per job (2) + one at the top of each while-iteration: the batch
    # iteration, and the following one that finds no more jobs and returns (once=True).
    assert heartbeat_calls["n"] == 4


def test_stop_event_checked_between_jobs(monkeypatch, tmp_path):
    """A stop signalled while processing the first job of a batch must prevent
    the second job in that same batch from being picked up."""
    heartbeat_path = tmp_path / "heartbeat"
    monkeypatch.setenv("DM_WORKER_HEARTBEAT_FILE", str(heartbeat_path))

    jobs = [
        QueueJob(id="1", topic="t", payload={}, attempts=0, max_attempts=3, dedupe_key=None),
        QueueJob(id="2", topic="t", payload={}, attempts=0, max_attempts=3, dedupe_key=None),
    ]

    class _OneBatchQueue:
        def __init__(self):
            self.served = False
            self.acked = []

        def claim_jobs(self, *, worker_id, limit):
            if self.served:
                return []
            self.served = True
            return jobs

        def ack(self, *, job_id, worker_id):
            self.acked.append(job_id)

    queue = _OneBatchQueue()
    stop_event = threading.Event()

    def _process_and_stop(job):
        stop_event.set()

    monkeypatch.setattr(m, "_get_queue_manager", lambda: queue)
    monkeypatch.setattr(m, "_process_queue_job", _process_and_stop)

    m._run_queue_worker_loop(stop_event=stop_event, once=True)

    assert queue.acked == ["1"]
