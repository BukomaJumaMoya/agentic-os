#!/usr/bin/env python3
"""
Run one registered workflow, out of band, as a tracked job.

Spawned detached by a slash command. The command acknowledges immediately and
this process does the work, because a proposal takes 30 seconds or more and a
20-minute workflow would be unthinkable to run inside a chat handler.

Generalises enquiry_runner: that one knew only about the enquiry pipeline and
called run_workflow directly. This one takes a workflow name and inputs, so a
new workflow needs no new runner.

Because nothing waits on the exit code, every outcome announces itself. Success
is announced by the workflow's own approval prompt; everything else is a message
to the operator. A dropped job is worse than a visible error: somebody is
waiting either way, and silence is the one result that guarantees nobody acts.

Cancellation and timeout are not failures of the work -- they are decisions
about it -- so they are reported as their own statuses rather than folded into
"failed".

Input: one JSON object on stdin --
    {"job_id": "...", "workflow": "proposal", "inputs": {...},
     "user_id": "..."}
Output: JSON lines on stdout, for the runner log only. Nobody reads them live;
/apr_jobs reads the job record instead.
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator import jobs  # noqa: E402
from orchestrator import telegram_approval as _tg  # noqa: E402
from orchestrator import workflow as _workflow  # noqa: E402


def _log(**fields):
    fields["at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(fields), flush=True)


def _notify(user_id, text):
    """Best-effort DM. Never raises: this is already the error path.

    Goes through send_direct_message, which is covered by the outbound block,
    so a job spawned from a test cannot message anyone.
    """
    try:
        recipient = user_id or _tg._allowed_user_id()
        result = _tg.send_direct_message(recipient, text)
        _log(event="notify", sent=result.get("sent"),
             delivery_status=result.get("delivery_status"))
        return result
    except Exception as e:
        _log(event="notify_failed", error=f"{type(e).__name__}: {e}")
        return {"sent": False, "delivery_status": "not_attempted"}


def _short(job_id):
    return str(job_id or "")[:8]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        _log(event="bad_input", error=str(e))
        return 1

    job_id = data.get("job_id")
    name = data.get("workflow")
    inputs = data.get("inputs") or {}
    user_id = data.get("user_id")

    if not job_id or not name:
        _log(event="bad_input", error="job_id and workflow are both required")
        return 1

    definition = _workflow.get(name)
    if definition is None:
        jobs.finish(job_id, jobs.FAILED, error=f"unknown workflow: {name}")
        _notify(user_id, f"No workflow named {name!r}. Nothing was run.")
        _log(event="unknown_workflow", workflow=name)
        return 1

    jobs.start(job_id, pid=os.getpid())
    _log(event="start", job=job_id, workflow=name, pid=os.getpid())

    try:
        result = _workflow.run(name, inputs, job=job_id)
    except jobs.Cancelled as e:
        # A decision about the work, not a failure of it.
        jobs.mark_cancelled(job_id, e.step)
        _log(event="cancelled", job=job_id, step=e.step)
        _notify(user_id,
                f"Job {_short(job_id)} cancelled at {e.step}.\n\n"
                "Nothing was filed for approval and nothing was sent.")
        return 0
    except jobs.TimedOut as e:
        jobs.finish(job_id, jobs.FAILED, error=str(e))
        _log(event="timeout", job=job_id, step=e.step, elapsed=e.elapsed)
        _notify(user_id,
                f"Job {_short(job_id)} ran past its {e.budget}s budget and was "
                f"stopped at {e.step}.\n\n"
                "Nothing was filed for approval and nothing was sent.")
        return 1
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        jobs.finish(job_id, jobs.FAILED, error=detail)
        _log(event="crashed", job=job_id, error=detail,
             traceback=traceback.format_exc()[:2000])
        _notify(user_id,
                f"Job {_short(job_id)} crashed before finishing.\n\n"
                f"Error: {detail}\n\n"
                "Nothing was sent to anyone.")
        return 1

    status = result.get("status")
    request_id = (result.get("approval_request") or {}).get("request_id")
    result_ref = {"request_id": request_id,
                  "artifact": definition.artifact} if request_id else None

    # The workflow's own success statuses decide what counts as done, rather
    # than this runner keeping a second opinion about it.
    if status in definition.success_statuses:
        jobs.finish(job_id, jobs.SUCCEEDED, result_ref=result_ref)
        _log(event="finished", job=job_id, status=status, request_id=request_id)
        if status == "approval_prompt_undelivered":
            # An artifact exists and is waiting, but the prompt carrying it did
            # not arrive. Without this the job is silently stuck.
            _notify(user_id,
                    f"Job {_short(job_id)} produced a {definition.artifact}, "
                    "but the approval prompt could not be delivered.\n\n"
                    f"Request: {request_id}\n\n"
                    f"Approve with:\n/apr_approve {request_id}\n"
                    f"Reject with:\n/apr_reject {request_id}")
        return 0

    jobs.finish(job_id, jobs.FAILED, result_ref=result_ref,
                error=result.get("error") or status)
    _log(event="finished", job=job_id, status=status)
    reason = {
        "llm_unavailable": "No model provider was reachable.",
        "llm_invalid_output": "The model replied, but the draft failed validation.",
        "invalid_input": "The request was rejected before anything ran.",
        "unknown_workflow": "No such workflow.",
        "authority_violation": "The workflow invoked an agent it does not declare.",
        "artifact_missing": "The workflow reported success but produced nothing.",
    }.get(status, f"The workflow ended with status: {status}")
    _notify(user_id,
            f"Job {_short(job_id)} produced no {definition.artifact}, so "
            "nothing is waiting for approval.\n\n"
            f"{reason}\n"
            f"Detail: {(result.get('error') or 'none')[:300]}\n\n"
            "Nothing was sent to anyone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
