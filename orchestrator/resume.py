#!/usr/bin/env python3
"""
Resume an approved external action after human approval.

This module checks the approval decision for a given request ID and,
if approved, executes the external action and reports the result.

It is intended to be invoked by Hermes or OpenClaw after JUMA approves
an action via Telegram or another channel.
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "orchestrator"))

from orchestrator.approval import resume_if_approved  # noqa: E402
from orchestrator.external_action import execute_external_action  # noqa: E402


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    request_id = data.get("request_id")
    if not request_id:
        print(json.dumps({"error": "Missing request_id"}))
        sys.exit(1)

    resume = resume_if_approved(request_id)
    if not resume.get("approved"):
        print(json.dumps({"ok": False, "request_id": request_id, "reason": resume.get("reason", "not_approved")}))
        sys.exit(2)

    approval_dir = BASE / ".approval"
    request_path = approval_dir / f"{request_id}.request.json"
    proposal = {}
    if request_path.exists():
        try:
            proposal = json.loads(request_path.read_text()).get("proposal", {})
        except Exception:
            pass

    external_action = {
        "request_id": request_id,
        "type": data.get("action_type") or proposal.get("type") or "send_proposal",
        "idempotency_key": request_id,
    }
    result = execute_external_action(external_action, proposal=proposal)
    print(json.dumps({"ok": result.get("ok"), "request_id": request_id, "result": result}, indent=2))
    sys.exit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
