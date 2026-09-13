#!/usr/bin/env python3
"""
Run the flagship workflow for one enquiry, out of band.

Spawned detached by the /apr_enquiry slash command. A proposal takes 30 seconds
or more -- one model call plus up to three specialist subprocesses -- and a
slash-command handler that blocked that long would time out in the gateway and
leave the user staring at nothing. So the handler acknowledges immediately and
this process does the work.

Because nothing is waiting on the exit code, every outcome has to announce
itself. On success the approval prompt is sent by run_workflow through the path
that already works. On any other outcome this sends a plain message to the same
Telegram DM saying what happened. A dropped enquiry is worse than a visible
error: the client is waiting for a reply either way, and silence is the one
result that guarantees nobody acts.

Input: one JSON object on stdin -- {"user_id": "...", "enquiry": "..."}.
Output: a JSON line on stdout, for the runner log only. Nobody reads it live.
"""

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator import llm as _llm  # noqa: E402
from orchestrator import telegram_approval as _tg  # noqa: E402
from orchestrator.flagship import run_workflow  # noqa: E402

# Same ceiling as the model boundary. The slash command checks this too; this
# is the copy that matters, because it is the one next to the model call.
MAX_ENQUIRY_CHARS = _llm.MAX_ENQUIRY_CHARS


def _log(**fields):
    fields["at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(fields), flush=True)


def _notify(user_id, text):
    """Best-effort DM. Never raises: this is the error path already."""
    try:
        recipient = user_id or _tg._allowed_user_id()
        result = _tg.send_direct_message(recipient, text)
        _log(event="notify", sent=result.get("sent"),
             delivery_status=result.get("delivery_status"))
        return result
    except Exception as e:
        _log(event="notify_failed", error=f"{type(e).__name__}: {e}")
        return {"sent": False, "delivery_status": "not_attempted"}


def _first_line(text, limit=90):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit] + ("…" if len(line) > limit else "")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        _log(event="bad_input", error=str(e))
        return 1

    user_id = data.get("user_id")
    enquiry = (data.get("enquiry") or "").strip()

    if not enquiry:
        _notify(user_id, "Enquiry was empty. Nothing was run.")
        _log(event="empty_enquiry")
        return 1

    if len(enquiry) > MAX_ENQUIRY_CHARS:
        _notify(user_id,
                f"Enquiry is {len(enquiry)} characters, above the "
                f"{MAX_ENQUIRY_CHARS} limit. Refusing rather than truncating — "
                f"send a shorter version and nothing will be lost.")
        _log(event="too_long", length=len(enquiry))
        return 1

    _log(event="start", chars=len(enquiry), preview=_first_line(enquiry))

    try:
        result = run_workflow(enquiry, approval_mode=True)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        _log(event="crashed", error=detail, traceback=traceback.format_exc()[:2000])
        _notify(user_id,
                "The workflow crashed before producing a proposal.\n\n"
                f"Enquiry: {_first_line(enquiry)}\n"
                f"Error: {detail}\n\n"
                "Nothing was sent to anyone. Re-send the enquiry once the cause "
                "is fixed.")
        return 1

    status = result.get("status")
    request_id = (result.get("approval_request") or {}).get("request_id")
    _log(event="finished", status=status, request_id=request_id)

    if status == "awaiting_approval":
        # The approval prompt has already arrived by the existing path. Saying
        # so again here would just be a second notification for one event.
        return 0

    if status == "approval_prompt_undelivered":
        # A proposal exists and is waiting, but the prompt carrying it did not
        # arrive. Without this the enquiry is silently stuck.
        _notify(user_id,
                "A proposal was drafted, but the approval prompt could not be "
                "delivered.\n\n"
                f"Enquiry: {_first_line(enquiry)}\n"
                f"Request: {request_id}\n\n"
                f"Approve it with:\n/apr_approve {request_id}\n"
                f"Reject it with:\n/apr_reject {request_id}")
        return 0

    # Everything else: no proposal object exists. llm_unavailable,
    # llm_invalid_output, or a validation error on the enquiry itself.
    reason = {
        "llm_unavailable": "No model provider was reachable.",
        "llm_invalid_output": "The model replied, but the draft failed validation.",
    }.get(status, f"The workflow ended with status: {status}")

    _notify(user_id,
            "No proposal was produced, so nothing is waiting for approval.\n\n"
            f"Enquiry: {_first_line(enquiry)}\n"
            f"{reason}\n"
            f"Detail: {(result.get('error') or 'none')[:300]}\n\n"
            "No draft was saved and nothing was sent to anyone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
