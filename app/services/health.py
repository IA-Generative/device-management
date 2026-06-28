"""Per-pod health metrics — stdlib only (no psutil dependency).

Surfaced on the admin debug "fleet" table: resident memory, cgroup limit, load
average, cpu count, and a cumulative request counter incremented by the HTTP
middleware. All readers are best-effort: a missing /proc or /sys file (e.g. on
macOS dev) degrades to None rather than raising.
"""
from __future__ import annotations

import os
import sys
import threading

# ── Cumulative request counter (incremented by the security_headers middleware) ──
_req_lock = threading.Lock()
_req_count = 0


def incr_request() -> None:
    """Count one served request. Cheap; called from the HTTP middleware."""
    global _req_count
    with _req_lock:
        _req_count += 1


def requests_total() -> int:
    with _req_lock:
        return _req_count


def _read_rss_bytes() -> int | None:
    """Resident set size of the current process, in bytes."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # value is in kB
    except OSError:
        pass
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kB, macOS/BSD report bytes.
        return int(rss) if sys.platform == "darwin" else int(rss) * 1024
    except Exception:
        return None


def _read_mem_limit_bytes() -> int | None:
    """Container memory limit (cgroup v2 then v1), or None if unlimited/unknown."""
    try:
        with open("/sys/fs/cgroup/memory.max", encoding="ascii") as f:
            v = f.read().strip()
            return int(v) if v and v != "max" else None
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", encoding="ascii") as f:
            v = int(f.read().strip())
            return v if v < (1 << 62) else None  # sentinel = unlimited
    except (OSError, ValueError):
        return None
    return None


def read_health() -> dict:
    """Snapshot of this pod's health metrics for config_pod_state."""
    try:
        load1: float | None = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load1 = None
    return {
        "rss_bytes": _read_rss_bytes(),
        "mem_limit_bytes": _read_mem_limit_bytes(),
        "load1": load1,
        "cpu_count": os.cpu_count(),
        "requests_total": requests_total(),
    }
