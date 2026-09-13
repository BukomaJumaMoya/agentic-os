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
