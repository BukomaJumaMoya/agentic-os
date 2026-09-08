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
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent
APPROVAL_DIR = BASE / ".approval"


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

    decision_path = APPROVAL_DIR / f"{request_id}.decision.json"
    if not decision_path.exists():
        return {"ok": False, "reason": "missing_decision"}
    try:
        decision = json.loads(decision_path.read_text())
    except Exception as e:
        return {"ok": False, "reason": f"invalid_decision: {e}"}
    if decision.get("approved") is not True:
        return {"ok": False, "reason": "not_approved", "decision": decision}

    executed = False
    result = None
    verification = {"request_id": request_id, "attempted_at": datetime.now(timezone.utc).isoformat()}

    if action_type == "send_proposal":
        proposal_text = proposal.get("body") or proposal.get("subject") or ""
        message = proposal_text if proposal_text else json.dumps(proposal, indent=2)[:4000]
        payload = {
            "to": os.getenv("OPENCLAW_ALLOWED_USER_ID", "1360833951"),
            "text": message,
            "approval_request_id": request_id,
        }
        result = _post("/message/send", payload)
        executed = bool(result.get("ok")) or int(result.get("status", 0)) == 200
        verification.update({
            "channel": "telegram",
            "proposal_id": proposal.get("proposal_id"),
            "sent": executed,
        })

    elif action_type == "send_message":
        text = proposal.get("text") or json.dumps(proposal, indent=2)[:4000]
        payload = {
            "to": os.getenv("OPENCLAW_ALLOWED_USER_ID", "1360833951"),
            "text": text,
            "approval_request_id": request_id,
        }
        result = _post("/message/send", payload)
        executed = bool(result.get("ok")) or int(result.get("status", 0)) == 200
        verification.update({
            "channel": "telegram",
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

    try:
        decision["execution"] = verification
        decision_path.write_text(json.dumps(decision, indent=2))
    except Exception:
        pass

    return {
        "ok": executed,
        "request_id": request_id,
        "action_type": action_type,
        "verification": verification,
        "result": result,
    }


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    try:
        from orchestrator.telegram_approval import _post as _post_impl
        return _post_impl(path, payload, timeout=timeout)
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


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
