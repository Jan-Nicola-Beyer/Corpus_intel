"""Lightweight in-process background job registry with SSE progress.

Roadmap ref: SINGLE_USER_ROADMAP P10.3.

No DB, no broker. A worker pool of threads, each running a callable that
periodically publishes progress updates through the shared `Job.publish()`.
Clients subscribe via SSE (`/api/jobs/{id}/events`).

Notes
-----
- Single-user app, so contention is low.
- Jobs are kept in memory for the life of the process. A snapshot is persisted
  to disk on every transition so a crash still leaves a recoverable trail for
  post-mortem, but running jobs are *not* resumed across restarts.
- Cancellation is cooperative: the callable must check `job.cancel_requested`
  between units of work.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from corpus_intel.constants import SESSION_DIR

log = logging.getLogger("corpus_intel.jobs")

JOBS_FILE = os.path.join(SESSION_DIR, "jobs.json")
_MAX_JOBS_ON_DISK = 200
_lock = threading.Lock()
_jobs: Dict[str, "Job"] = {}
_workers: Dict[str, threading.Thread] = {}


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress_pct: float = 0.0
    message: str = ""
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    # non-serialised runtime bits:
    _subscribers: List["queue.Queue[str]"] = field(default_factory=list, repr=False)
    cancel_requested: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_subscribers", None)
        return d

    # ── progress / lifecycle ────────────────────────────────────────────────
    def publish(self, *, progress_pct: Optional[float] = None, message: Optional[str] = None) -> None:
        if progress_pct is not None:
            self.progress_pct = max(0.0, min(100.0, float(progress_pct)))
        if message is not None:
            self.message = str(message)
        self._broadcast()

    def _broadcast(self) -> None:
        payload = json.dumps({
            "id": self.id, "kind": self.kind, "status": self.status,
            "progress_pct": self.progress_pct, "message": self.message,
            "error": self.error,
        })
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def subscribe(self) -> "queue.Queue[str]":
        q: queue.Queue[str] = queue.Queue(maxsize=200)
        with _lock:
            self._subscribers.append(q)
        # send the current snapshot immediately
        try:
            q.put_nowait(json.dumps({
                "id": self.id, "kind": self.kind, "status": self.status,
                "progress_pct": self.progress_pct, "message": self.message,
                "error": self.error,
            }))
        except Exception:
            pass
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with _lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


# ─── Registry ───────────────────────────────────────────────────────────────
def _persist() -> None:
    try:
        os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
        items = [j.to_dict() for j in sorted(_jobs.values(), key=lambda x: x.created_at, reverse=True)]
        if len(items) > _MAX_JOBS_ON_DISK:
            items = items[:_MAX_JOBS_ON_DISK]
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, JOBS_FILE)
    except Exception:
        log.exception("jobs persist failed")


def _load_initial() -> None:
    if not os.path.exists(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f) or []
    except Exception:
        return
    for r in raw:
        try:
            # Anything that was running when the process died is now error'd.
            if r.get("status") in ("queued", "running"):
                r["status"] = "error"
                r["error"] = r.get("error") or "interrupted by process exit"
                r["finished_at"] = r.get("finished_at") or datetime.now().isoformat(timespec="seconds")
            j = Job(**{k: v for k, v in r.items() if k in Job.__dataclass_fields__ and k != "_subscribers"})
            _jobs[j.id] = j
        except Exception:
            continue


_load_initial()


def submit(kind: str, fn: Callable[["Job"], Dict[str, Any]], *, params: Optional[Dict[str, Any]] = None) -> Job:
    """Schedule `fn(job)` on a worker thread. Returns the Job immediately."""
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        created_at=datetime.now().isoformat(timespec="seconds"),
        params=dict(params or {}),
    )
    with _lock:
        _jobs[job.id] = job
    _persist()

    def _runner():
        job.status = "running"
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job._broadcast()
        _persist()
        try:
            result = fn(job)
            if job.cancel_requested:
                job.status = "cancelled"
            else:
                job.status = "done"
                job.result = result or {}
            job.progress_pct = 100.0
        except Exception as e:
            log.exception("job %s (%s) failed", job.id, job.kind)
            job.status = "error"
            job.error = str(e)
        finally:
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job._broadcast()
            _persist()

    th = threading.Thread(target=_runner, name=f"job-{job.id}", daemon=True)
    _workers[job.id] = th
    th.start()
    return job


def get(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def list_jobs(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.to_dict() for j in items[:limit]]


def clear_finished() -> int:
    """Remove all done/error/cancelled jobs from the registry. Returns count cleared."""
    with _lock:
        finished_ids = [jid for jid, j in _jobs.items() if j.status in ("done", "error", "cancelled")]
        for jid in finished_ids:
            _jobs.pop(jid, None)
            _workers.pop(jid, None)
    _persist()
    return len(finished_ids)


def request_cancel(job_id: str) -> bool:
    j = _jobs.get(job_id)
    if not j:
        return False
    if j.status not in ("queued", "running"):
        return False
    j.cancel_requested = True
    j.publish(message="cancellation requested…")
    return True


def sse_events(job_id: str, *, heartbeat_seconds: float = 15.0):
    """Generator yielding SSE-formatted strings for a single job.

    Terminates when the job is no longer in a running state *and* its final
    event has been delivered.
    """
    j = _jobs.get(job_id)
    if not j:
        yield f"event: error\ndata: {json.dumps({'error': 'unknown job'})}\n\n"
        return
    q = j.subscribe()
    last_heartbeat = time.time()
    try:
        while True:
            try:
                payload = q.get(timeout=1.0)
                yield f"data: {payload}\n\n"
            except queue.Empty:
                # heartbeat to keep proxies happy
                if time.time() - last_heartbeat >= heartbeat_seconds:
                    yield ": keepalive\n\n"
                    last_heartbeat = time.time()
            # terminal statuses — flush and stop
            if j.status in ("done", "error", "cancelled"):
                # drain any remaining events quickly
                try:
                    while True:
                        payload = q.get_nowait()
                        yield f"data: {payload}\n\n"
                except queue.Empty:
                    pass
                break
    finally:
        j.unsubscribe(q)
