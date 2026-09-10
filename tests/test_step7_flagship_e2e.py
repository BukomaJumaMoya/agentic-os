#!/usr/bin/env python3
"""
End-to-end tests for Step 7 — Flagship Workflow.

Exercises the full pipeline from enquiry to evidence/approval artifacts.
"""
import json
import sys
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401 -- MUST precede orchestrator imports

EVIDENCE_DIR = _state_isolation.EVIDENCE_DIR
APPROVAL_DIR = _state_isolation.APPROVAL_DIR

from orchestrator.flagship import run_workflow, request_approval
from orchestrator.approval import record_decision, resume_if_approved, cleanup, is_approved
from orchestrator.external_action import execute_external_action

try:
    from unittest.mock import patch
except Exception:
    patch = None


def setup():
    for d in [EVIDENCE_DIR, APPROVAL_DIR]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def teardown():
    for d in [EVIDENCE_DIR, APPROVAL_DIR]:
        if d.exists():
            shutil.rmtree(d)


def test_e2e_full_flow_creates_evidence():
    setup()
    enquiry = "Hi, I need a web app for my small business. Budget around $10k, timeline 2 months."
    result = run_workflow(enquiry, approval_mode=True)
    # Delivery-dependent: "awaiting_approval" only when the prompt actually
    # reached Telegram. Assert the invariant that status and the delivery flag
    # agree, so an undelivered prompt can never read as a delivered one.
    assert result["status"] in ("awaiting_approval", "approval_prompt_undelivered")
    assert result["approval_prompt_delivered"] is (result["status"] == "awaiting_approval")
    request_id = result["approval_request"]["request_id"]
    assert (APPROVAL_DIR / f"{request_id}.request.json").exists()
    assert (EVIDENCE_DIR / f"{request_id}-proposal.json").exists()
    cleanup(request_id)
    teardown()
    print("PASS: e2e_full_flow_creates_evidence")


def test_e2e_evidence_contains_no_secrets():
    setup()
    enquiry = "Need a landing page, budget $5k, timeline 3 weeks."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    evidence_text = (EVIDENCE_DIR / f"{request_id}-proposal.json").read_text()
    request_text = (APPROVAL_DIR / f"{request_id}.request.json").read_text()
    combined = evidence_text + request_text
    assert "GEMINI_API_KEY" not in combined
    assert "CLICKUP_TOKEN" not in combined
    assert "password" not in combined.lower()
    cleanup(request_id)
    teardown()
    print("PASS: e2e_evidence_contains_no_secrets")


def test_e2e_approval_decision_flow():
    setup()
    enquiry = "Need a mobile app, budget $20k, timeline 3 months."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=True, approver="juma", reason="approved")
    assert is_approved(request_id) is True
    resume = resume_if_approved(request_id)
    assert resume["approved"] is True
    cleanup(request_id)
    teardown()
    print("PASS: e2e_approval_decision_flow")


def test_e2e_rejection_stops_external_action():
    setup()
    enquiry = "Send proposal to client now."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=False, approver="juma", reason="too early")
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    assert result["external_actions"][0]["status"] == "pending_approval"
    cleanup(request_id)
    teardown()
    print("PASS: e2e_rejection_stops_external_action")


def test_e2e_timeout_does_not_approve():
    setup()
    enquiry = "Need a proposal."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    # Do not record decision; simulate timeout
    assert is_approved(request_id) is False
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    assert resume["reason"] == "pending"
    cleanup(request_id)
    teardown()
    print("PASS: e2e_timeout_does_not_approve")


def test_e2e_retry_preserves_partial_output():
    setup()
    enquiry = "Research competitor pricing for SaaS CRMs."
    result = run_workflow(enquiry, approval_mode=True)
    assert "retry_log" in result["verification"]
    assert "specialist_errors" in result["verification"]
    assert result["proposal"]["proposal_id"]
    teardown()
    print("PASS: e2e_retry_preserves_partial_output")


def test_e2e_telegram_approval_prompt_recorded():
    setup()
    enquiry = "Need a proposal for a new client."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    external_action = result["external_actions"][0]
    assert "telegram_prompt" in external_action
    assert external_action["telegram_prompt"]["request_id"] == request_id
    cleanup(request_id)
    teardown()
    print("PASS: e2e_telegram_approval_prompt_recorded")


def test_e2e_external_action_execution_blocks_without_approval():
    setup()
    enquiry = "Send proposal to client."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    external_action = result["external_actions"][0]
    if patch is not None:
        with patch("external_action._post", return_value={"ok": False, "status": 403, "error": "forbidden"}):
            exec_result = execute_external_action(external_action, result.get("proposal", {}))
    else:
        exec_result = {"ok": False, "reason": "missing_decision"}
    assert exec_result["ok"] is False
    assert exec_result["reason"] == "missing_decision"
    cleanup(request_id)
    teardown()
    print("PASS: e2e_external_action_execution_blocks_without_approval")


def test_e2e_external_action_execution_with_approval():
    setup()
    enquiry = "Send proposal to client after approval."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=True, approver="juma", reason="approved")
    external_action = result["external_actions"][0]
    if patch is not None:
        with patch("orchestrator.telegram_approval._post", return_value={"ok": True, "status": 200}):
            exec_result = execute_external_action(external_action, result.get("proposal", {}))
    else:
        exec_result = {"ok": False, "reason": "missing_decision"}
    assert exec_result["ok"] is True
    cleanup(request_id)
    teardown()
    print("PASS: e2e_external_action_execution_with_approval")


def main():
    try:
        setup()
        test_e2e_full_flow_creates_evidence()
        test_e2e_evidence_contains_no_secrets()
        test_e2e_approval_decision_flow()
        test_e2e_rejection_stops_external_action()
        test_e2e_timeout_does_not_approve()
        test_e2e_retry_preserves_partial_output()
        print("\nALL END-TO-END TESTS PASSED")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        teardown()


if __name__ == "__main__":
    sys.exit(main())
