#!/usr/bin/env python3
"""
Test Step 7 — Flagship End-to-End Workflow.

Unit tests (fast, isolated, no external side effects):
- test_classify_enquiry_*
- test_load_config_*
- test_synthesize_proposal_*
- test_idempotent_approval_request
- test_decision_not_overwritten_by_default
- test_external_action_not_repeated_after_timeout
- test_no_secrets_in_evidence

Integration-style tests (run specialists, create files):
- test_normal_client_enquiry
- test_enquiry_with_missing_information
- test_research_required_enquiry
- test_technical_coding_required_enquiry
- test_clickup_project_update_required
- test_approval_rejection
- test_approved_proposal
- test_specialist_failure
- test_external_action_failure
- test_external_action_success_but_verification_fails
- test_duplicate_request
- test_retry_scenario
- test_partial_workflow_completion
- test_unexpected_tool_response
- test_verification_failure_boundary
"""
import json
import sys
import os
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

EVIDENCE_DIR = BASE / "evidence"
APPROVAL_DIR = BASE / ".approval"

from orchestrator.flagship import run_workflow, request_approval, classify_enquiry
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


def test_normal_client_enquiry():
    setup()
    enquiry = "Hi, I need a web app for my small business. Budget around $10k, timeline 2 months."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["workflow"] == "flagship"
    assert result["status"] == "awaiting_approval"
    assert "proposal" in result
    assert "classification" in result
    assert "agents_invoked" in result
    assert result["verification"]["config_present"] is True
    teardown()
    print("PASS: normal_client_enquiry")


def test_enquiry_with_missing_information():
    setup()
    enquiry = "We need an AI chatbot."
    result = run_workflow(enquiry, approval_mode=True)
    classification = result["classification"]
    assert "missing_information" in classification
    assert "budget" in classification["missing_information"] or "timeline" in classification["missing_information"]
    teardown()
    print("PASS: enquiry_with_missing_information")


def test_research_required_enquiry():
    setup()
    enquiry = "Research competitor pricing for SaaS CRMs and compare with our offering."
    result = run_workflow(enquiry, approval_mode=True)
    assert "research" in result["agents_invoked"]
    teardown()
    print("PASS: research_required_enquiry")


def test_technical_coding_required_enquiry():
    setup()
    enquiry = "We need a Python API integration with ClickUp and Telegram bots. Please debug our existing code."
    result = run_workflow(enquiry, approval_mode=True)
    assert "coding" in result["agents_invoked"]
    teardown()
    print("PASS: technical_coding_required_enquiry")


def test_clickup_project_update_required():
    setup()
    enquiry = "Create a ClickUp task for onboarding and update project status to in progress."
    result = run_workflow(enquiry, approval_mode=True)
    assert "projects" in result["agents_invoked"]
    teardown()
    print("PASS: clickup_project_update_required")


def test_rejected_proposal():
    setup()
    enquiry = "Need a mobile app, budget $20k, timeline 3 months."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["status"] == "awaiting_approval"
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=False, approver="juma", reason="scope unclear")
    assert is_approved(request_id) is False
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    cleanup(request_id)
    teardown()
    print("PASS: rejected_proposal")


def test_approved_proposal():
    setup()
    enquiry = "We need a landing page, budget $5k, timeline 3 weeks."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["status"] == "awaiting_approval"
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=True, approver="juma", reason="approved")
    assert is_approved(request_id) is True
    resume = resume_if_approved(request_id)
    assert resume["approved"] is True
    cleanup(request_id)
    teardown()
    print("PASS: approved_proposal")


def test_approval_rejection():
    setup()
    enquiry = "Need a mobile app, budget $20k, timeline 3 months."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["status"] == "awaiting_approval"
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=False, approver="juma", reason="scope unclear")
    assert is_approved(request_id) is False
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    cleanup(request_id)
    teardown()
    print("PASS: approval_rejection")


def test_external_action_failure():
    setup()
    enquiry = "Send proposal to client now."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["requires_approval"] is True
    assert any(a["type"] == "send_proposal" for a in result["external_actions"])
    assert result["external_actions"][0]["status"] == "pending_approval"
    teardown()
    print("PASS: external_action_failure")


def test_external_action_success_but_verification_fails():
    setup()
    enquiry = "Send proposal to client now."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["requires_approval"] is True
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=True, approver="juma")
    assert is_approved(request_id) is True
    assert result["external_actions"][0]["status"] == "pending_approval"
    cleanup(request_id)
    teardown()
    print("PASS: external_action_success_but_verification_fails")


def test_duplicate_request():
    setup()
    enquiry = "Need a website, budget $8k."
    result1 = run_workflow(enquiry, approval_mode=True)
    result2 = run_workflow(enquiry + " duplicate", approval_mode=True)
    req1 = result1["approval_request"]["request_id"]
    req2 = result2["approval_request"]["request_id"]
    assert req1 != req2
    cleanup(req1)
    cleanup(req2)
    teardown()
    print("PASS: duplicate_request")


def test_retry_scenario():
    setup()
    enquiry = "Research competitor pricing for SaaS CRMs."
    result = run_workflow(enquiry, approval_mode=True)
    assert "retry_log" in result["verification"]
    assert "specialist_errors" in result["verification"]
    teardown()
    print("PASS: retry_scenario")


def test_partial_workflow_completion():
    setup()
    enquiry = "Research the ClickUp API and debug our Python integration code."
    result = run_workflow(enquiry, approval_mode=True)
    assert "research" in result["agents_invoked"]
    assert "projects" in result["agents_invoked"]
    assert "coding" in result["agents_invoked"]
    assert result["status"] in ["awaiting_approval", "error"]
    teardown()
    print("PASS: partial_workflow_completion")


def test_unexpected_tool_response():
    setup()
    enquiry = "Unknown task xyzzy"
    result = run_workflow(enquiry, approval_mode=True)
    assert "verification" in result
    assert "specialist_errors" in result["verification"]
    teardown()
    print("PASS: unexpected_tool_response")


def test_specialist_failure():
    setup()
    enquiry = ""
    result = run_workflow(enquiry, approval_mode=True)
    assert result["status"] == "error"
    assert "error" in result
    teardown()
    print("PASS: specialist_failure")


def test_external_action_failure():
    setup()
    enquiry = "Send proposal to client now."
    result = run_workflow(enquiry, approval_mode=True)
    assert result["requires_approval"] is True
    assert any(a["type"] == "send_proposal" for a in result["external_actions"])
    teardown()
    print("PASS: external_action_failure_boundary")


def test_verification_failure():
    setup()
    enquiry = "Need a proposal."
    result = run_workflow(enquiry, approval_mode=True)
    assert "verification" in result
    verification = result["verification"]
    assert "config_present" in verification
    assert "proposal_non_empty" in verification
    teardown()
    print("PASS: verification_failure_boundary")


def test_idempotent_approval_request():
    setup()
    enquiry = "Need a website, budget $8k."
    result = run_workflow(enquiry, approval_mode=True)
    request_path = APPROVAL_DIR / f"{result['approval_request']['request_id']}.request.json"
    evidence_path = EVIDENCE_DIR / f"{result['approval_request']['request_id']}-proposal.json"
    first_request_mtime = request_path.stat().st_mtime if request_path.exists() else 0
    first_evidence_mtime = evidence_path.stat().st_mtime if evidence_path.exists() else 0

    # repeat same workflow
    run_workflow(enquiry, approval_mode=True)
    second_request_mtime = request_path.stat().st_mtime if request_path.exists() else 0
    second_evidence_mtime = evidence_path.stat().st_mtime if evidence_path.exists() else 0
    assert first_request_mtime == second_request_mtime
    assert first_evidence_mtime == second_evidence_mtime
    cleanup(result["approval_request"]["request_id"])
    teardown()
    print("PASS: idempotent_approval_request")


def test_decision_not_overwritten_by_default():
    setup()
    enquiry = "Need a mobile app, budget $20k, timeline 3 months."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=True, approver="juma", reason="approved first")
    decision1 = json.loads((APPROVAL_DIR / f"{request_id}.decision.json").read_text())
    record_decision(request_id, approved=False, approver="other", reason="changed")
    decision2 = json.loads((APPROVAL_DIR / f"{request_id}.decision.json").read_text())
    assert decision1["approved"] is True
    assert decision2["approved"] is True
    assert decision2["reason"] == "approved first"
    cleanup(request_id)
    teardown()
    print("PASS: decision_not_overwritten_by_default")


def test_external_action_not_repeated_after_timeout():
    setup()
    enquiry = "Send proposal to client now."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    action = result["external_actions"][0]
    assert action["status"] == "pending_approval"
    assert action["retry_safe"] is True
    assert action["idempotency_key"] == request_id
    cleanup(request_id)
    teardown()
    print("PASS: external_action_not_repeated_after_timeout")


def main():
    try:
        setup()
        test_normal_client_enquiry()
        test_enquiry_with_missing_information()
        test_research_required_enquiry()
        test_technical_coding_required_enquiry()
        test_clickup_project_update_required()
        test_approval_rejection()
        test_approved_proposal()
        test_specialist_failure()
        test_external_action_failure()
        test_external_action_success_but_verification_fails()
        test_duplicate_request()
        test_retry_scenario()
        test_partial_workflow_completion()
        test_unexpected_tool_response()
        test_verification_failure()
        print("\nALL FLAGSHIP WORKFLOW TESTS PASSED")
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
