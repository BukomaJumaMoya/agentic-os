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

EVIDENCE_DIR = BASE / "evidence"
APPROVAL_DIR = BASE / ".approval"

from orchestrator.flagship import run_workflow, request_approval
from orchestrator.approval import record_decision, resume_if_approved, cleanup, is_approved


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
    assert result["status"] == "awaiting_approval"
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
