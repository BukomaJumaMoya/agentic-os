#!/usr/bin/env python3
"""
Telegram command parser and action for OpenClaw inbound messages.

This module is intended to be invoked by OpenClaw when it receives a
Telegram message from JUMA. It parses approval commands and updates
the approval decision files so the flagship workflow can resume.

Supported commands:
- APPROVE <request_id> [reason...]
- REJECT <request_id> [reason...]
- STATUS <request_id>
- LIST
- RESUME <request_id>

Security:
- Only the configured allowlisted user may approve/reject
  (OPENCLAW_ALLOWED_USER_ID, or telegram.allowed_user_id in config/juma.json).
- Commands are case-insensitive.
- Returns structured JSON suitable for OpenClaw tool output.
"""

import json
from datetime import datetime, timezone
import sys
from pathlib import Path

# Put the repo root ahead of this file's own directory on sys.path, BEFORE the
# first orchestrator import. Running as a script ("python
# orchestrator/telegram_commands.py") puts orchestrator/ on sys.path[0] -- the
# subprocess cwd is never added -- and orchestrator/orchestrator.py then shadows
# the orchestrator package, so "from orchestrator import approval" binds the
# wrong module. A try/except around the import cannot recover from that: the
# failed import leaves the shadowing module cached in sys.modules, so the retry
# resolves to it again regardless of sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import approval as _approval  # noqa: E402
from orchestrator import jobs as _jobs  # noqa: E402

ConfigError = _approval.ConfigError
BASE = _approval.BASE


def _resolve_request_dir() -> Path:
    return _approval.APPROVAL_DIR


def _decision_path(request_id: str) -> Path:
    return _approval.decision_path(request_id)


def _request_path(request_id: str) -> Path:
    return _approval.request_path(request_id)


# A chat reply nobody can scroll is a reply nobody reads.
MAX_JOBS_LISTED = 10
# Same ceiling as orchestrator/llm.py MAX_ENQUIRY_CHARS.
MAX_ENQUIRY_CHARS = 8000


def _age(iso_time):
    """Human-readable age, so a listing does not carry raw timestamps."""
    if not iso_time:
        return "-"
    try:
        then = datetime.fromisoformat(str(iso_time))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "-"
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _job_summary(entry):
    """One job, reduced to what a chat reply should carry.

    Identifiers, status and the last checkpoint. Never the enquiry, never the
    proposal, never the evidence -- the same rule that keeps /apr_list from
    dumping a 30 KB proposal into a chat window.
    """
    out = {
        "job": (entry.get("job_id") or "")[:8],
        "workflow": entry.get("workflow"),
        "status": entry.get("status"),
        "age": _age(entry.get("created_at")),
    }
    if entry.get("status") == "running":
        out["at"] = entry.get("last_step")
        if entry.get("cancel_requested"):
            out["cancelling"] = True
    if entry.get("request_id"):
        out["request"] = entry["request_id"][:8]
    return out


def _approval_state(request_id):
    """Live approval state for a request, from the one module that owns it."""
    try:
        return _approval.decision_state(request_id)
    except Exception as e:
        return {"state": "invalid", "detail": str(e)}


def _summarize(rec, status, decision=None):
    """One approval request, reduced to what a chat reply should carry.

    LIST and STATUS used to return rec["proposal"] whole. That object holds the
    full proposal body, config_used, the model's raw reply, and the evidence
    block -- which includes third-party research text scraped from the open web.
    A single request file is ~30 KB. Pushing that into a Telegram reply dumps
    client-facing prose and untrusted page content into a chat window on every
    listing, where it is easy to mistake a draft for something already sent.

    The identifiers are what a listing is for; the body is what /apr_approve is
    for. Anything not named here is deliberately withheld.
    """
    proposal = rec.get("proposal") or {}
    request_id = rec.get("request_id")
    state = _approval_state(request_id)
    summary = {
        "request_id": request_id,
        "status": status,
        # The live state, which is not the same thing as "a decision exists".
        # An approval that has expired or been used still has a decision file;
        # reporting it as simply "approved" is how a stale approval looks
        # actionable.
        "approval_state": state.get("state"),
        "timestamp": rec.get("timestamp"),
        "subject": proposal.get("subject"),
    }
    if state.get("state") == "approved":
        summary["expires_at"] = state.get("expires_at")
        summary["seconds_remaining"] = state.get("seconds_remaining")
    elif state.get("state") == "expired":
        summary["expired_at"] = state.get("expires_at")
        summary["expired_for_seconds"] = state.get("expired_for_seconds")
        summary["actionable"] = False
    elif state.get("state") == "consumed":
        summary["consumed_at"] = state.get("consumed_at")
        summary["actionable"] = False
    if decision:
        summary["decision"] = {
            "approved": decision.get("approved"),
            "approver": decision.get("approver"),
            "reason": decision.get("reason"),
            "timestamp": decision.get("timestamp"),
        }
    return summary


def _list_pending():
    pending = []
    if not _resolve_request_dir().exists():
        return pending
    for req in sorted(_resolve_request_dir().glob("*.request.json")):
        try:
            rec = json.loads(req.read_text())
        except Exception:
            continue
        rid = rec.get("request_id")
        if not rid:
            continue
        state = _approval_state(rid)
        if state.get("state") in ("pending", "expired", "consumed"):
            # Expired and consumed requests still appear, labelled. Hiding them
            # would make a request that can no longer be acted on look like it
            # was never made.
            label = {"pending": "pending", "expired": "expired",
                     "consumed": "already_used"}[state["state"]]
            pending.append(_summarize(rec, label))
            continue
        try:
            decision = json.loads(_decision_path(rid).read_text())
        except Exception:
            continue
        pending.append({
            "request_id": rid,
            "status": "approved" if decision.get("approved") else "rejected",
            "timestamp": decision.get("timestamp"),
            "reason": decision.get("reason"),
            "proposal": rec.get("proposal"),
        })
    return pending


def handle_telegram_command(user_id: str, text: str) -> dict:
    try:
        allowed = _approval.allowed_user_id()
    except ConfigError as e:
        # Fail closed: with no configured allowlist nobody is authorised.
        return {"ok": False, "error": "allowlist_not_configured", "detail": str(e)}
    if str(user_id) != allowed:
        # Do not echo the allowlisted id back to an unauthorised caller.
        return {"ok": False, "error": "not_allowed"}

    trimmed = text.strip()
    if not trimmed:
        return {"ok": False, "error": "empty_command"}

    parts = trimmed.split(maxsplit=2)
    command = parts[0].upper()
    request_id = parts[1] if len(parts) > 1 else None
    rest = parts[2] if len(parts) > 2 else ""

    if command in ("APPROVE", "REJECT"):
        approved = command == "APPROVE"
        if not request_id:
            return {"ok": False, "error": "missing_request_id",
                    "example": f"{command} <request_id>"}
        # Existence must be checked against the REQUEST record. The previous
        # code tested the DECISION file, which by definition does not exist for
        # a pending approval -- so APPROVE could never succeed.
        if not _request_path(request_id).exists():
            return {"ok": False, "error": "request_not_found", "request_id": request_id}
        before = _decision_path(request_id).exists()
        decision = _approval.record_decision(
            request_id, approved, approver="telegram-user",
            reason=rest or ("approved" if approved else "rejected"),
            channel="telegram",
        )
        if before:
            return {"ok": True, "status": "already_decided",
                    "request_id": request_id, "decision": decision}
        return {"ok": True, "status": "approved" if approved else "rejected",
                "request_id": request_id, "decision": decision}

    if command == "STATUS":
        if not request_id:
            return {"ok": False, "error": "missing_request_id", "example": "STATUS <request_id>"}
        req_path = _request_path(request_id)
        dec_path = _decision_path(request_id)
        if not req_path.exists():
            return {"ok": False, "error": "request_not_found", "request_id": request_id}
        rec = json.loads(req_path.read_text())
        # Summary only -- see _summarize. The proposal body is reached by acting
        # on the request, not by querying it.
        state = _approval_state(request_id)
        decision = state.get("decision")
        # "expired" and "already used" are distinct outcomes and are reported
        # as such, rather than collapsing into "approved" or "pending".
        status = {"approved": "approved", "rejected": "rejected",
                  "expired": "expired", "consumed": "already_used",
                  "invalid": "invalid"}.get(state.get("state"), "pending")
        return {"ok": True, **_summarize(rec, status, decision)}

    if command == "LIST":
        return {"ok": True, "pending": _list_pending()}

    if command == "JOBS":
        # Summary only, same discipline as LIST: identifiers and progress, never
        # inputs, bodies or evidence. Capped so a long history cannot flood the
        # chat -- a reply nobody can scroll is a reply nobody reads.
        limit = MAX_JOBS_LISTED
        if request_id and request_id.isdigit():
            limit = max(1, min(int(request_id), MAX_JOBS_LISTED))
        listed = _jobs.list_jobs(limit=limit)
        return {"ok": True, "jobs": [_job_summary(j) for j in listed],
                "shown": len(listed), "limit": limit}

    if command == "CANCEL":
        if not request_id:
            return {"ok": False, "error": "missing_job_id",
                    "example": "CANCEL <job_id>"}
        job_id, problem = _jobs.resolve_id(request_id)
        if problem == "not_found":
            return {"ok": False, "outcome": "not_found", "job_id": request_id}
        if problem and problem.startswith("ambiguous:"):
            # Refuse rather than guess which job was meant.
            return {"ok": False, "outcome": "ambiguous_job_id",
                    "job_id": request_id,
                    "matches": problem.split(":", 1)[1].split(","),
                    "detail": "more than one job starts with that prefix; "
                              "send more characters"}
        if problem:
            return {"ok": False, "outcome": problem, "job_id": request_id}
        return _jobs.request_cancel(job_id)

    if command == "ENQUIRY":
        # Everything after the verb is the enquiry text.
        enquiry = (trimmed.split(maxsplit=1)[1] if len(trimmed.split(maxsplit=1)) > 1
                   else "").strip()
        if not enquiry:
            return {"ok": False, "error": "missing_enquiry",
                    "example": "ENQUIRY <the client's message>"}
        if len(enquiry) > MAX_ENQUIRY_CHARS:
            # Refuse, never truncate: the tail is the part the client bothered
            # to explain.
            return {"ok": False, "error": "enquiry_too_long",
                    "length": len(enquiry), "limit": MAX_ENQUIRY_CHARS,
                    "detail": "refusing rather than truncating; send a shorter "
                              "version and nothing is lost"}
        definition = None
        try:
            from orchestrator import workflow as _workflow
            definition = _workflow.get("proposal")
        except Exception:
            definition = None
        job = _jobs.create(
            "proposal", {"enquiry": enquiry}, requested_by=user_id,
            max_runtime_seconds=getattr(definition, "max_runtime_seconds", None))
        spawned = _jobs.spawn(job["job_id"], "proposal", {"enquiry": enquiry},
                              user_id=user_id)
        if not spawned.get("spawned"):
            _jobs.finish(job["job_id"], _jobs.FAILED,
                         error=spawned.get("reason", "spawn failed"))
            return {"ok": False, "error": "spawn_failed",
                    "job_id": job["job_id"], "detail": spawned.get("reason")}
        return {"ok": True, "status": "queued", "job_id": job["job_id"],
                "short_id": job["job_id"][:8],
                "preview": job["inputs_preview"],
                "chars": len(enquiry)}

    if command == "RESUME":
        if not request_id:
            return {"ok": False, "error": "missing_request_id", "example": "RESUME <request_id>"}
        from orchestrator.approval import resume_if_approved
        resume = resume_if_approved(request_id)
        if resume.get("approved"):
            return {"ok": True, "status": "ready", "request_id": request_id, "resume": resume}
        return {"ok": True, "status": "not_ready", "request_id": request_id, "resume": resume}

    return {"ok": False, "error": "unknown_command",
            "supported": ["APPROVE", "REJECT", "STATUS", "LIST", "RESUME",
                          "JOBS", "CANCEL", "ENQUIRY"]}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    # No fallback: the caller must state who sent the message. Defaulting to
    # the allowlisted id would authorise every anonymous invocation.
    user_id = data.get("user_id")
    if not user_id:
        print(json.dumps({"ok": False, "error": "missing_user_id"}))
        sys.exit(2)
    text = data.get("text") or data.get("message") or ""
    result = handle_telegram_command(user_id, text)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
