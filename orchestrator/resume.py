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
sys.path.insert(0, str(BASE))

from orchestrator import approval as _approval  # noqa: E402
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

    request_path = _approval.request_path(request_id)
    if not request_path.exists():
        print(json.dumps({"ok": False, "request_id": request_id,
                          "reason": "request_record_missing"}))
        sys.exit(2)
    try:
        proposal = json.loads(request_path.read_text()).get("proposal", {})
    except Exception as e:
        # Swallowing this left proposal={}, which execute_external_action
        # then sent as the literal string "{}" while reporting ok:True.
        print(json.dumps({"ok": False, "request_id": request_id,
                          "reason": f"request_record_unreadable: {e}"}))
        sys.exit(2)

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
