"""Lightweight reachability probe for api.anthropic.com.

The probe is cached for PROBE_TTL seconds so we don't hit the API on every
state refresh. Consumers:

- /api/ai/health endpoint surfaces the current status to the frontend
- claude_client reports connection errors to _mark_down so subsequent calls
  short-circuit for PROBE_TTL seconds (avoids long timeouts on repeated
  failures)
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

PROBE_TTL = 30.0   # seconds
HOST = "api.anthropic.com"
PORT = 443


@dataclass
class HealthStatus:
    reachable: bool
    reason: str
    checked_at: float
    last_error: str = ""


_lock = threading.Lock()
_cached: Optional[HealthStatus] = None


def _probe() -> HealthStatus:
    """Cheap TCP probe. We don't authenticate — we just need to know if the
    host is reachable. If DNS or TCP fails, the API is unreachable."""
    try:
        with socket.create_connection((HOST, PORT), timeout=4.0):
            return HealthStatus(True, "ok", time.time())
    except (socket.gaierror, OSError) as e:
        return HealthStatus(False, "unreachable", time.time(), last_error=str(e))
    except Exception as e:  # noqa: BLE001
        return HealthStatus(False, "error", time.time(), last_error=str(e))


def status(force: bool = False) -> HealthStatus:
    """Return cached status, or re-probe if expired."""
    global _cached
    with _lock:
        now = time.time()
        if _cached and not force and (now - _cached.checked_at) < PROBE_TTL:
            return _cached
        _cached = _probe()
        return _cached


def mark_down(reason: str, err: str = "") -> None:
    """Stamp the cache as unreachable (called by claude_client on hard failure)."""
    global _cached
    with _lock:
        _cached = HealthStatus(False, reason, time.time(), last_error=err)


def mark_up() -> None:
    """Stamp the cache as reachable (called after a successful API call)."""
    global _cached
    with _lock:
        _cached = HealthStatus(True, "ok", time.time())


def to_dict() -> dict:
    s = status()
    return {
        "reachable": s.reachable,
        "reason": s.reason,
        "checked_at": s.checked_at,
        "last_error": s.last_error,
        "ttl_seconds": PROBE_TTL,
    }
