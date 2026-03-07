from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    event_id: str
    dedupe_key: str
    created_at: int
    payload: dict


class ClusterStore:
    def __init__(self, name: str) -> None:
        self.name = name
        self.available = True
        self._by_dedupe: dict[str, Event] = {}

    def write(self, event: Event) -> Event:
        if not self.available:
            raise RuntimeError(f"cluster {self.name} unavailable")
        existing = self._by_dedupe.get(event.dedupe_key)
        if existing:
            return existing
        self._by_dedupe[event.dedupe_key] = event
        return event

    def has_dedupe(self, dedupe_key: str) -> bool:
        return dedupe_key in self._by_dedupe

    def all_dedupe_keys(self) -> set[str]:
        return set(self._by_dedupe.keys())


class ReplicationLink:
    def __init__(self, source: ClusterStore, target: ClusterStore, lag_seconds: int) -> None:
        self.source = source
        self.target = target
        self.lag_seconds = lag_seconds
        self._inflight: list[tuple[int, Event]] = []

    def publish(self, event: Event, now: int) -> None:
        self._inflight.append((now + self.lag_seconds, event))

    def tick(self, now: int) -> None:
        still_waiting: list[tuple[int, Event]] = []
        for deliver_at, event in self._inflight:
            if deliver_at <= now:
                if self.target.available:
                    self.target.write(event)
                else:
                    still_waiting.append((deliver_at, event))
            else:
                still_waiting.append((deliver_at, event))
        self._inflight = still_waiting


class LockingQueueModel:
    def __init__(self, lock_ttl_seconds: int = 30) -> None:
        self.lock_ttl_seconds = lock_ttl_seconds
        self.now = 0
        self.jobs: dict[str, dict] = {}

    def enqueue(self, job_id: str) -> None:
        self.jobs[job_id] = {
            "status": "pending",
            "locked_at": None,
            "lock_owner": None,
        }

    def claim(self, worker_id: str) -> list[str]:
        claimed: list[str] = []
        for job_id, job in self.jobs.items():
            stale = job["locked_at"] is not None and (self.now - job["locked_at"]) >= self.lock_ttl_seconds
            if job["status"] == "pending" or stale:
                job["status"] = "processing"
                job["locked_at"] = self.now
                job["lock_owner"] = worker_id
                claimed.append(job_id)
        return claimed

    def advance(self, seconds: int) -> None:
        self.now += seconds


def test_simulated_az_outage_failover_preserves_idempotence():
    now = 0
    cluster_a = ClusterStore("az-a")
    cluster_b = ClusterStore("az-b")
    a_to_b = ReplicationLink(cluster_a, cluster_b, lag_seconds=5)
    b_to_a = ReplicationLink(cluster_b, cluster_a, lag_seconds=5)

    keys_a = {f"device-{i}" for i in range(1, 6)}
    for idx, key in enumerate(sorted(keys_a), start=1):
        event = Event(event_id=f"a-{idx}", dedupe_key=key, created_at=now, payload={"v": idx})
        cluster_a.write(event)
        a_to_b.publish(event, now)

    now += 5
    a_to_b.tick(now)
    assert cluster_b.all_dedupe_keys() == keys_a

    cluster_a.available = False
    keys_b = {f"device-{i}" for i in range(6, 9)}
    for idx, key in enumerate(sorted(keys_b), start=1):
        event = Event(event_id=f"b-{idx}", dedupe_key=key, created_at=now, payload={"v": idx})
        cluster_b.write(event)
        b_to_a.publish(event, now)

    cluster_a.available = True
    now += 5
    b_to_a.tick(now)

    expected = keys_a | keys_b
    assert cluster_a.all_dedupe_keys() == expected
    assert cluster_b.all_dedupe_keys() == expected


def test_worker_loss_reclaims_stale_locks_after_ttl():
    queue = LockingQueueModel(lock_ttl_seconds=30)
    queue.enqueue("job-1")

    first = queue.claim("worker-a")
    assert first == ["job-1"]

    queue.advance(10)
    assert queue.claim("worker-b") == []

    queue.advance(20)
    reclaimed = queue.claim("worker-b")
    assert reclaimed == ["job-1"]


def test_inter_cluster_replication_lag_respects_rpo_target():
    now = 0
    rpo_target_seconds = 30
    cluster_a = ClusterStore("primary")
    cluster_b = ClusterStore("secondary")
    link = ReplicationLink(cluster_a, cluster_b, lag_seconds=20)

    event = Event(event_id="evt-1", dedupe_key="user-1", created_at=now, payload={"state": "active"})
    cluster_a.write(event)
    link.publish(event, now)
    now += 20
    link.tick(now)

    assert cluster_b.has_dedupe("user-1")
    assert (now - event.created_at) <= rpo_target_seconds


def test_post_outage_convergence_within_rto_target():
    now = 0
    rto_target_seconds = 300
    cluster_a = ClusterStore("cluster-a")
    cluster_b = ClusterStore("cluster-b")
    a_to_b = ReplicationLink(cluster_a, cluster_b, lag_seconds=25)
    b_to_a = ReplicationLink(cluster_b, cluster_a, lag_seconds=25)

    cluster_b.available = False
    for idx in range(10):
        event = Event(event_id=f"a-{idx}", dedupe_key=f"client-{idx}", created_at=now, payload={"i": idx})
        cluster_a.write(event)
        a_to_b.publish(event, now)

    now += 60
    a_to_b.tick(now)
    assert cluster_b.all_dedupe_keys() == set()

    cluster_b.available = True
    a_to_b.tick(now)
    now += 25
    a_to_b.tick(now)
    for key in sorted(cluster_b.all_dedupe_keys()):
        event = Event(event_id=f"back-{key}", dedupe_key=key, created_at=now, payload={"echo": True})
        b_to_a.publish(event, now)
    now += 25
    b_to_a.tick(now)

    assert cluster_a.all_dedupe_keys() == cluster_b.all_dedupe_keys()
    assert now <= rto_target_seconds
