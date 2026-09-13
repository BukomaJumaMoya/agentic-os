#!/usr/bin/env python3
"""
External Action Executor — executes approved external actions and verifies outcomes.

Supported actions:
- send_proposal: deliver a prepared proposal to an external channel
- send_message: send an arbitrary approved message
- create_clickup_task: create a ClickUp task via the projects agent wrapper
- github_create_issue: create a GitHub issue via gh CLI

Each execution records verification state and never retries automatically
without a new explicit approval.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

# Repo root ahead of this file's own directory, before the first orchestrator
# import -- see the note in telegram_commands.py. orchestrator/orchestrator.py
# shadows the package when this module runs as a script, and a try/except cannot
# recover because the failed import poisons sys.modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import approval as _approval  # noqa: E402

ConfigError = _approval.ConfigError
BASE = _approval.BASE
APPROVAL_DIR = _approval.APPROVAL_DIR


_DATE_TOLERANCE_DAYS = 2
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
_TEXT_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATELINE_RE = re.compile(r"^\s*\**\s*date\s*\**\s*:", re.IGNORECASE)
_HOURLY_RATE_RE = re.compile(
    r"\$\s*[\d,]+(?:\s*[-–—]\s*\$?\s*[\d,]+)?\s*(?:/|\s*per\s+)\s*(?:hr|hour)",
    re.IGNORECASE)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _check_date(violations, line, month, day, year, reference):
    try:
        found = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return
    drift = abs((found - reference).days)
    if drift > _DATE_TOLERANCE_DAYS:
        violations.append({
            "field": "date",
            "expected": reference.date().isoformat(),
            "found": found.date().isoformat(),
            "detail": f"dateline is {drift} days from the proposal date: {line.strip()[:80]}",
        })


def check_proposal_invariants(proposal: dict, text: str, config: dict = None) -> dict:
    """Fail closed on proposal content that contradicts config/juma.json.

    Identity is the thing a drafting model gets wrong silently. The archived
    Acme draft was signed "Alex Mercer", carried no business name, and was
    datelined five months stale -- and passed every check that existed, because
    no check looked at the content at all.

    Returns {"ok": bool, "violations": [{field, expected, found, detail}]}.
    """
    config = _approval.load_config() if config is None else config
    violations = []
    body = text or ""

    # An unreadable config cannot authorise a send.
    expected_name = (config.get("name") or "").strip()
    expected_business = (config.get("business") or "").strip()
    if not expected_name or not expected_business:
        violations.append({
            "field": "config", "expected": "name and business in config/juma.json",
            "found": None,
            "detail": "cannot verify proposal identity against an empty or unreadable config",
        })
        return {"ok": False, "violations": violations}

    lowered = body.lower()
    if expected_name.lower() not in lowered:
        violations.append({"field": "name", "expected": expected_name, "found": None,
                           "detail": "author name from config does not appear in the proposal"})
    if expected_business.lower() not in lowered:
        violations.append({"field": "business", "expected": expected_business, "found": None,
                           "detail": "business name from config does not appear in the proposal"})

    # Rate: conditional. Generated proposals carry no rate line, so this fires
    # only when the text states an hourly rate -- which must then match config.
    expected_rate = (config.get("default_rate") or "").strip()
    found_rates = _HOURLY_RATE_RE.findall(body) or []
    if found_rates and expected_rate:
        want = _digits(expected_rate)
        for found in found_rates:
            if _digits(found) != want:
                violations.append({"field": "default_rate", "expected": expected_rate,
                                   "found": found.strip(),
                                   "detail": "stated hourly rate does not match config"})

    # Date: only the dateline / header block, not milestone dates in the body.
    reference = datetime.now(timezone.utc)
    created = proposal.get("created_at")
    if created:
        try:
            reference = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except Exception:
            pass
    lines = body.splitlines()
    header = [ln for i, ln in enumerate(lines) if i < 15 or _DATELINE_RE.match(ln)]
    for line in header:
        for month, day, year in _TEXT_DATE_RE.findall(line):
            _check_date(violations, line,
                        _MONTHS[month.lower()], int(day), int(year), reference)
        for year, month, day in _ISO_DATE_RE.findall(line):
            _check_date(violations, line, int(month), int(day), int(year), reference)

    return {"ok": not violations, "violations": violations}


def _project_base() -> Path:
    return BASE


def _exec(agent_name: str, payload: dict, retries: int = 2):
    try:
        from orchestrator.orchestrator import invoke
        return invoke(agent_name, payload, retries=retries)
    except Exception as e:
        return None, str(e)


def execute_external_action(external_action: dict, proposal: dict = None) -> dict:
    action_type = (external_action.get("type") or "").lower()
    request_id = external_action.get("request_id")
    proposal = proposal or {}

    if not request_id:
        return {"ok": False, "reason": "missing_request_id"}

    # Single gate: orchestrator.approval owns what "approved" means.
    decision_path = _approval.decision_path(request_id)
    resume = _approval.resume_if_approved(request_id)
    if not resume.get("approved"):
        return {"ok": False, "reason": resume.get("reason", "not_approved"),
                "decision": resume.get("decision")}
    decision = resume.get("decision") or {}

    executed = False
    result = None
    recipient = None
    verification = {"request_id": request_id, "attempted_at": datetime.now(timezone.utc).isoformat()}

    if action_type in ("send_proposal", "send_message"):
        try:
            recipient = _approval.allowed_user_id()
        except ConfigError as e:
            return {"ok": False, "request_id": request_id, "action_type": action_type,
                    "reason": f"allowlist not configured: {e}"}

    if action_type == "send_proposal":
        proposal_text = proposal.get("body") or proposal.get("subject") or ""
        message = proposal_text if proposal_text else json.dumps(proposal, indent=2)[:4000]
        invariants = check_proposal_invariants(proposal, message)
        if not invariants["ok"]:
            # Nothing is sent. An approval authorises an action, not whatever
            # text happened to be attached to it.
            return {"ok": False, "request_id": request_id, "action_type": action_type,
                    "reason": "content_invariant_violation",
                    "violations": invariants["violations"]}
        result = _send_telegram(recipient, message)
        executed = bool(result.get("sent"))
        verification.update({
            "channel": "telegram",
            "proposal_id": proposal.get("proposal_id"),
            "delivery_status": result.get("delivery_status"),
            "conversation_ref": result.get("conversation_ref"),
            "sent": executed,
        })

    elif action_type == "send_message":
        text = proposal.get("text") or json.dumps(proposal, indent=2)[:4000]
        result = _send_telegram(recipient, text)
        executed = bool(result.get("sent"))
        verification.update({
            "channel": "telegram",
            "delivery_status": result.get("delivery_status"),
            "conversation_ref": result.get("conversation_ref"),
            "sent": executed,
        })

    elif action_type == "create_clickup_task":
        task_payload = proposal.get("payload") or proposal
        output, error = _exec("projects", {
            "action": "create_task",
            "name": task_payload.get("name") or task_payload.get("subject") or "Approved Task",
            "description": task_payload.get("description") or task_payload.get("body") or "",
        })
        executed = error is None and isinstance(output, dict) and output.get("status") == "ok"
        verification.update({
            "agent": "projects",
            "output": output,
            "error": error,
            "created": executed,
        })

    elif action_type == "github_create_issue":
        title = proposal.get("title") or "Approved work item"
        body = proposal.get("body") or json.dumps(proposal, indent=2)
        try:
            proc = subprocess.run(
                ["gh", "issue", "create", "--title", title, "--body", body],
                capture_output=True,
                text=True,
                timeout=120,
            )
            executed = proc.returncode == 0
            verification.update({
                "agent": "gh",
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "created": executed,
            })
        except Exception as e:
            executed = False
            verification.update({"agent": "gh", "error": str(e), "created": False})

    else:
        return {"ok": False, "reason": f"unsupported_external_action_type: {action_type}"}

    verification["executed"] = executed
    verification["completed_at"] = datetime.now(timezone.utc).isoformat()

    # Single use. An approval authorises ONE action; consuming it means a
    # replay, a retry, or a second caller holding the same request id cannot
    # act on the same consent twice.
    #
    # Consumed after success, not before, so a transport failure does not burn
    # an approval the operator would have to grant again. The residual window is
    # the gap between the action landing and the rename: two callers executing
    # concurrently could both succeed before either consumes. Nothing in this
    # system executes concurrently today -- external actions run one at a time
    # from the approval path -- and burning an approval on a failed send was
    # judged the worse trade.
    if executed:
        consumption = _approval.consume_decision(request_id)
    else:
        consumption = {"consumed": False, "reason": "not_executed"}
    verification["approval_consumed"] = consumption.get("consumed", False)
    verification["approval_consumption"] = consumption

    audit_write_error = None
    try:
        decision["execution"] = verification
        decision_path.write_text(json.dumps(decision, indent=2))
    except Exception as e:
        # The action has already run. Deliberately do NOT flip ok: reporting
        # failure here invites a retry and a duplicate side effect. Surface it
        # loudly instead -- executed, but unrecorded.
        audit_write_error = str(e)
        verification["audit_write_failed"] = audit_write_error

    return {
        "ok": executed,
        "request_id": request_id,
        "action_type": action_type,
        "audit_write_error": audit_write_error,
        "verification": verification,
        "result": result,
    }


def _send_telegram(user_id: str, text: str) -> dict:
    """Outbound send. Delegates to the one implementation in telegram_approval
    so the conversationRef cache and delivery_status vocabulary are shared."""
    try:
        from orchestrator.telegram_approval import send_direct_message
        return send_direct_message(user_id, text)
    except Exception as e:
        return {"sent": False, "delivery_status": "invoke_failed",
                "conversation_ref": None, "reason": str(e),
                "result": {"error": str(e)}}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    external_action = data.get("external_action") or {}
    proposal = data.get("proposal") or {}
    if not external_action:
        print(json.dumps({"error": "Missing external_action"}))
        sys.exit(1)

    result = execute_external_action(external_action, proposal)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
