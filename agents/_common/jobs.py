#!/usr/bin/env python3
"""The job pattern: no MCP call blocks for minutes.

WHY
---
Research is two HTTP round trips plus a model call; a coding task is a whole
agent working through a repository. The second one takes minutes. An MCP tool
that blocks for minutes is not merely slow -- it is broken in four separate
ways at once:

  - Hermes' MCP client has a per-call timeout, and a coding task will exceed
    it. The call fails while the work continues, orphaned.
  - Telegram gets nothing until it finishes, so the user cannot tell the
    difference between "compiling" and "hung".
  - Nothing can be cancelled, because the only handle on the work is a call
    that has not returned.
  - Hermes cannot do anything else meanwhile, including answering the user's
    question about what it is doing.

So long work returns a job id immediately and the caller polls. The shape is
the one already proven in the archived orchestrator/jobs.py: start, status,
result -- kept because Hermes' prompt already understands it and because it is
the minimum that supports cancellation.

DESIGN NOTES
------------
Threads, not processes: the work is I/O-bound (HTTP to OpenRouter, a
subprocess for the coding agent) and a thread can be cooperatively cancelled
through a flag the worker checks. Cancellation is cooperative by design --
killing a thread mid-write to a git repository is worse than letting it finish
the current step.

Results are kept in memory with a retention cap. This process is spawned by
Hermes per session and dies with it; persisting job results to disk would
outlive the conversation that can interpret them, and those results contain
client material.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import errors

# How many finished jobs to keep. Enough to poll a few tasks comfortably;
# small enough that a long session does not accumulate client data in RAM.
MAX_RETAINED = 40

# A finished job older than this is dropped even if the cap is not reached.
RETENTION_SECONDS = 3600


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"          # running | done | failed | cancelled
    created: float = field(default_factory=time.time)
    finished: float | None = None
    progress: str = "starting"
    result: Any = None
    error: dict | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        return round((self.finished or time.time()) - self.created, 1)

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def note(self, text: str) -> None:
        """Called by the worker to report where it has got to."""
        self.progress = str(text)[:300]


class JobRegistry:
    def __init__(self, audit=None):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.audit = audit

    def start(self, kind: str, worker: Callable[[Job], Any]) -> Job:
        """Run `worker(job)` on a thread. Returns the Job immediately."""
        job = Job(id=f"{kind}-{uuid.uuid4().hex[:12]}", kind=kind)
        with self._lock:
            self._reap()
            self._jobs[job.id] = job

        def run() -> None:
            try:
                value = worker(job)
                if job.cancelled():
                    job.status = "cancelled"
                    job.progress = "cancelled by caller"
                else:
                    job.status = "done"
                    job.result = value
                    job.progress = "complete"
            except errors.AgentError as exc:
                job.status = "failed"
                job.error = {"error": exc.code,
                             "detail": errors.redact(exc.detail),
                             "retryable": exc.retryable}
                job.progress = "failed"
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = {
                    "error": "internal_error",
                    "detail": f"{job.kind} job failed with {type(exc).__name__}. "
                              f"Details are in this agent's audit log.",
                    "retryable": False,
                }
                job.progress = "failed"
                if self.audit:
                    self.audit.write("job_crash", job_id=job.id, kind=job.kind,
                                     exception=type(exc).__name__,
                                     traceback=traceback.format_exc())
            finally:
                job.finished = time.time()
                if self.audit:
                    self.audit.write("job_finished", job_id=job.id, kind=job.kind,
                                     status=job.status, seconds=job.elapsed)

        thread = threading.Thread(target=run, name=job.id, daemon=True)
        thread.start()
        if self.audit:
            self.audit.write("job_started", job_id=job.id, kind=kind)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(str(job_id or "").strip())
        if job is None:
            raise errors.AgentError(
                "unknown_job",
                f"no job with id {job_id!r}. Job ids are per-process and are "
                f"dropped after {RETENTION_SECONDS // 60} minutes or once "
                f"{MAX_RETAINED} newer jobs have finished.",
            )
        return job

    def status(self, job_id: str) -> dict:
        job = self.get(job_id)
        out = {
            "ok": True,
            "job_id": job.id,
            "kind": job.kind,
            "status": job.status,
            "progress": job.progress,
            "elapsed_seconds": job.elapsed,
            "finished": job.finished is not None,
        }
        if job.status == "failed" and job.error:
            out["error_preview"] = job.error.get("error")
        return out

    def result(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.status == "running":
            return {
                "ok": False,
                "error": "not_finished",
                "detail": f"job {job.id} is still running ({job.progress}, "
                          f"{job.elapsed}s elapsed). Poll get_status again.",
                "retryable": True,
                "status": "running",
            }
        if job.status == "failed":
            return {"ok": False, "status": "failed", **(job.error or {})}
        if job.status == "cancelled":
            return {"ok": False, "status": "cancelled", "error": "cancelled",
                    "detail": f"job {job.id} was cancelled"}
        return {"ok": True, "status": "done", "job_id": job.id,
                "elapsed_seconds": job.elapsed, "result": job.result}

    def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.status != "running":
            return {"ok": True, "job_id": job.id, "status": job.status,
                    "detail": "job had already finished; nothing to cancel"}
        job._cancel.set()
        job.note("cancellation requested")
        if self.audit:
            self.audit.write("job_cancel_requested", job_id=job.id)
        return {"ok": True, "job_id": job.id, "status": "cancelling",
                "detail": "cancellation is cooperative: the job stops at its "
                          "next checkpoint rather than being killed mid-step"}

    def _reap(self) -> None:
        """Drop old finished jobs. Caller holds the lock."""
        now = time.time()
        finished = [j for j in self._jobs.values() if j.finished is not None]
        stale = [j.id for j in finished if now - (j.finished or now) > RETENTION_SECONDS]
        for job_id in stale:
            self._jobs.pop(job_id, None)

        finished = sorted((j for j in self._jobs.values() if j.finished is not None),
                          key=lambda j: j.finished or 0)
        while len(finished) > MAX_RETAINED:
            self._jobs.pop(finished.pop(0).id, None)
