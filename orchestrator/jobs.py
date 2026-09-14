#!/usr/bin/env python3
"""
Job records: what is running, how far it got, and how to stop it.

A job is the durable trace of one workflow run. The detached runner owns the
record; a human owns the decision to stop it. They never write the same file.

WHY A SEPARATE CANCEL FILE
--------------------------
The runner writes status and progress continuously. A cancel request arrives
from a different process at an arbitrary moment. Sharing one file means a
read-modify-write race in which one side's update is silently lost -- and the
update most likely to be lost is the one that stops work.

So cancellation is signalled by creating <id>.cancel. The command only creates
it; the runner only reads it. Single writer each way, no locking, no lost
update. The same shape as the decision/consumed pair in approval.py.

WHAT CANCELLATION CAN AND CANNOT DO
-----------------------------------
Cooperative and checkpoint-based. The runner checks at defined points: before
and after each agent, before synthesis, and before filing the approval request.
It CANNOT interrupt an in-flight model call or agent subprocess, so the worst
case latency is one agent step.

It also cannot undo a send. Once an external action has executed and consumed
its approval, cancellation is refused as too_late_already_executed. That state
is derived from the approval record rather than tracked here, because the
approval record is what actually decides whether an action was authorised -- a
second copy of that truth would eventually disagree with it.

The runner's PID is recorded so a forceful kill is possible later. It is
deliberately not implemented: killing mid-write can leave torn approval state,
and not doing that is most of what this system is for.

CLIENT DATA
-----------
A job stores a short preview and a digest of its inputs, never the inputs. An
enquiry is client material and .approval/ already holds it in full; a second
copy in a second directory is a second thing to leak and a second thing to
forget to delete.
"""

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import approval as _approval  # noqa: E402

JOBS_DIR = _approval.STATE_ROOT / ".jobs"

PREVIEW_CHARS = 80
DEFAULT_MAX_RUNTIME_SECONDS = 300
# Hard ceiling regardless of whether the approval request still exists.
RETENTION_CEILING_DAYS = 30

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)


class Cancelled(Exception):
    """Raised at a checkpoint when cancellation was requested."""

    def __init__(self, step):
        super().__init__(f"cancelled at checkpoint: {step}")
        self.step = step


class TimedOut(Exception):
    """Raised at a checkpoint when the workflow outran its budget."""

    def __init__(self, step, elapsed, budget):
        super().__init__(
            f"timed out at checkpoint {step}: {elapsed:.0f}s of {budget}s budget")
        self.step = step
        self.elapsed = elapsed
        self.budget = budget


def _now():
    """Current UTC time. A function so tests can advance the clock."""
    return datetime.now(timezone.utc)


def _record_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _cancel_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.cancel"


def record_path(job_id: str) -> Path:
    return _record_path(job_id)


def cancel_path(job_id: str) -> Path:
    return _cancel_path(job_id)


def _write(record: dict) -> dict:
    """Atomic write: a reader never sees a half-written record."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = _record_path(record["job_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return record


def _read(job_id: str):
    path = _record_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _preview(inputs: dict) -> str:
    for key in ("enquiry", "prompt", "text", "query"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            flat = " ".join(value.split())
            return flat[:PREVIEW_CHARS] + ("..." if len(flat) > PREVIEW_CHARS else "")
    return ""


def _digest(inputs: dict) -> str:
    blob = json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:32]


def create(workflow: str, inputs: dict, requested_by=None,
           max_runtime_seconds: int = None) -> dict:
    """Register a queued job and return its record."""
    inputs = inputs if isinstance(inputs, dict) else {}
    now = _now()
    record = {
        "job_id": str(uuid.uuid4()),
        "workflow": workflow,
        "requested_by": str(requested_by) if requested_by else None,
        "status": QUEUED,
        "created_at": now.isoformat(),
        "started_at": None,
        "finished_at": None,
        "pid": None,
        # Never the inputs themselves -- see the module docstring.
        "inputs_preview": _preview(inputs),
        "inputs_digest": _digest(inputs),
        "max_runtime_seconds": int(max_runtime_seconds or DEFAULT_MAX_RUNTIME_SECONDS),
        "progress": [],
        "result_ref": None,
        "error": None,
        "cancelled_at": None,
        "cancelled_at_step": None,
    }
    return _write(record)


def get(job_id: str):
    return _read(job_id)


def start(job_id: str, pid: int = None) -> dict:
    record = _read(job_id)
    if record is None:
        return None
    record["status"] = RUNNING
    record["started_at"] = _now().isoformat()
    record["pid"] = int(pid) if pid else os.getpid()
    return _write(record)


def progress(job_id: str, step: str, message: str = "") -> dict:
    """Append one progress entry.

    Pulled by /apr_jobs, never pushed to a chat. Anything that sends per step
    will eventually send six times.
    """
    record = _read(job_id)
    if record is None:
        return None
    record.setdefault("progress", []).append({
        "at": _now().isoformat(), "step": step, "message": message,
    })
    return _write(record)


def finish(job_id: str, status: str, result_ref=None, error=None) -> dict:
    record = _read(job_id)
    if record is None:
        return None
    record["status"] = status
    record["finished_at"] = _now().isoformat()
    if result_ref is not None:
        record["result_ref"] = result_ref
    if error is not None:
        record["error"] = str(error)[:600]
    return _write(record)


def attach_result_ref(job_id: str, result_ref: dict) -> dict:
    """Link the job to the approval request it produced.

    Recorded as soon as the request exists, not at the end, so cancellation can
    consult the approval state even while the job is still running.
    """
    record = _read(job_id)
    if record is None:
        return None
    record["result_ref"] = result_ref
    return _write(record)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def cancel_requested(job_id: str) -> bool:
    return _cancel_path(job_id).exists()


def _already_executed(record: dict) -> bool:
    """Has this job's approval already authorised a completed action?

    Derived from the approval record. Once a decision is consumed, the action
    it authorised has happened and no amount of cancelling unsends it.
    """
    request_id = (record.get("result_ref") or {}).get("request_id")
    if not request_id:
        return False
    try:
        return _approval.decision_state(request_id)["state"] == "consumed"
    except Exception:
        return False


def request_cancel(job_id: str) -> dict:
    """Ask a job to stop. Writes the signal file; never edits the record."""
    record = _read(job_id)
    if record is None:
        return {"ok": False, "outcome": "not_found", "job_id": job_id}
    if record.get("status") in TERMINAL:
        return {"ok": False, "outcome": "already_finished", "job_id": job_id,
                "status": record["status"]}
    if _already_executed(record):
        return {"ok": False, "outcome": "too_late_already_executed",
                "job_id": job_id,
                "detail": "the approval was consumed by a completed external "
                          "action; that cannot be undone"}
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _cancel_path(job_id).write_text(
        json.dumps({"requested_at": _now().isoformat()}), encoding="utf-8")
    return {"ok": True, "outcome": "cancelling", "job_id": job_id,
            "detail": "the job stops at its next checkpoint; an agent already "
                      "running is not interrupted"}


def checkpoint(job_id: str, step: str, message: str = "") -> None:
    """A point at which a job may be stopped or time out.

    Raises Cancelled or TimedOut. A job_id of None makes this a no-op, so a
    workflow invoked outside the job runner behaves exactly as it did before
    jobs existed.
    """
    if not job_id:
        return
    record = _read(job_id)
    if record is None:
        return

    if cancel_requested(job_id):
        raise Cancelled(step)

    started = record.get("started_at") or record.get("created_at")
    budget = int(record.get("max_runtime_seconds") or DEFAULT_MAX_RUNTIME_SECONDS)
    if started:
        try:
            began = datetime.fromisoformat(str(started))
            if began.tzinfo is None:
                began = began.replace(tzinfo=timezone.utc)
            elapsed = (_now() - began).total_seconds()
            if elapsed > budget:
                raise TimedOut(step, elapsed, budget)
        except (TypeError, ValueError):
            pass

    progress(job_id, step, message)


def mark_cancelled(job_id: str, step: str) -> dict:
    record = _read(job_id)
    if record is None:
        return None
    record["cancelled_at"] = _now().isoformat()
    record["cancelled_at_step"] = step
    _write(record)
    return finish(job_id, CANCELLED,
                  error=f"cancelled at checkpoint: {step}")


# ---------------------------------------------------------------------------
# Listing and retention
# ---------------------------------------------------------------------------

def list_jobs(limit: int = 20) -> list:
    """Recent jobs, newest first. Summary only -- no inputs, no artifacts."""
    if not JOBS_DIR.exists():
        return []
    out = []
    for path in JOBS_DIR.glob("*.json"):
        if path.name.endswith(".json.tmp"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        last = (record.get("progress") or [])[-1:] or [{}]
        out.append({
            "job_id": record.get("job_id"),
            "workflow": record.get("workflow"),
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "finished_at": record.get("finished_at"),
            "preview": record.get("inputs_preview"),
            "last_step": last[0].get("step"),
            "last_message": last[0].get("message"),
            "request_id": (record.get("result_ref") or {}).get("request_id"),
            "cancel_requested": cancel_requested(record.get("job_id") or ""),
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


def resolve_id(prefix: str):
    """Resolve a short id prefix to exactly one job.

    Typing a full UUID on a phone is miserable, so a prefix is accepted. An
    ambiguous prefix is REFUSED rather than resolved to the newest match:
    guessing which job someone meant to cancel is precisely the wrong instinct.

    Returns (job_id, error) with exactly one of them set.
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return None, "missing_job_id"
    if not JOBS_DIR.exists():
        return None, "not_found"
    if _record_path(prefix).exists():
        return prefix, None
    matches = sorted(
        path.stem for path in JOBS_DIR.glob("*.json")
        if not path.name.endswith(".json.tmp") and path.stem.startswith(prefix)
    )
    if not matches:
        return None, "not_found"
    if len(matches) > 1:
        return None, "ambiguous:" + ",".join(m[:8] for m in matches[:5])
    return matches[0], None


# The runner is referenced by path, not imported: importing it would pull the
# whole workflow stack into the synchronous command path, which must stay fast
# and hard to break.
RUNNER_PATH = Path(__file__).resolve().parent / "job_runner.py"


def spawn(job_id: str, workflow: str, inputs: dict, user_id=None) -> dict:
    """Start the runner detached and return immediately.

    The job record already exists and is queued before this is called, so the
    acknowledgement can carry a real id and /apr_jobs shows the job at once
    rather than after the child happens to start.

    stdout and stderr go to a file rather than a pipe: an unread pipe on a
    detached child fills its buffer and blocks the writer, hanging the workflow
    partway with nothing to show for it.
    """
    import subprocess

    log_dir = _approval.STATE_ROOT / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        sink = open(log_dir / "job-runner.log", "a", encoding="utf-8")
    except Exception:
        sink = subprocess.DEVNULL

    kwargs = {"stdin": subprocess.PIPE, "stdout": sink, "stderr": sink,
              "cwd": str(RUNNER_PATH.parent.parent)}
    if os.name == "nt":
        # Detach from the console so the child outlives the gateway's handler.
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen([sys.executable, str(RUNNER_PATH)], **kwargs)
    except Exception as e:
        return {"spawned": False, "reason": f"{type(e).__name__}: {e}"}

    payload = json.dumps({"job_id": job_id, "workflow": workflow,
                          "inputs": inputs, "user_id": user_id})
    try:
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.close()
    except Exception as e:
        return {"spawned": False, "reason": f"could not send inputs: {e}"}
    return {"spawned": True, "pid": proc.pid}


def cleanup(job_id: str) -> None:
    for path in (_record_path(job_id), _cancel_path(job_id)):
        if path.exists():
            path.unlink()


def sweep(ceiling_days: int = RETENTION_CEILING_DAYS) -> dict:
    """Delete job records that no longer have anything to point at.

    Two rules, deliberately:

      - a job whose approval request has been cleaned up goes with it, so a job
        and its artifact disappear together rather than leaving a record that
        refers to something no longer on disk
      - anything older than the ceiling goes regardless, so a job that never
        produced a request cannot accumulate forever

    A running job is never swept, however old: deleting the record of work in
    flight would leave a process nobody can see or stop.
    """
    removed = {"orphaned": [], "expired": []}
    if not JOBS_DIR.exists():
        return removed
    cutoff = _now() - timedelta(days=ceiling_days)
    for path in sorted(JOBS_DIR.glob("*.json")):
        if path.name.endswith(".json.tmp"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        job_id = record.get("job_id")
        if not job_id or record.get("status") not in TERMINAL:
            continue

        created = record.get("created_at")
        try:
            born = datetime.fromisoformat(str(created))
            if born.tzinfo is None:
                born = born.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            born = None

        if born is not None and born < cutoff:
            cleanup(job_id)
            removed["expired"].append(job_id)
            continue

        request_id = (record.get("result_ref") or {}).get("request_id")
        if request_id and not _approval.request_path(request_id).exists():
            cleanup(job_id)
            removed["orphaned"].append(job_id)
    return removed
