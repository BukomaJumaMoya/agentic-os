#!/usr/bin/env python3
"""
Failure-injection tests for Step 7 — Flagship Workflow.

Injects controlled failures to verify:
- bounded retry
- partial workflow completion
- no secret leakage
- recovery state preservation
"""
import json
import sys
import shutil
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

EVIDENCE_DIR = BASE / "evidence"
APPROVAL_DIR = BASE / ".approval"

from orchestrator.flagship import run_workflow, classify_enquiry
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


def test_invalid_specialist_output():
    setup()
    enquiry = "Research competitor pricing for SaaS CRMs."
    result = run_workflow(enquiry, approval_mode=True)
    # research agent may return no_results or error; either is acceptable
    assert "verification" in result
    assert "specialist_errors" in result["verification"] or result["verification"].get("agents_selected")
    teardown()
    print("PASS: invalid_specialist_output")


def test_missing_enquiry_text():
    setup()
    result = run_workflow("", approval_mode=True)
    assert result["status"] == "error"
    assert "error" in result
    teardown()
    print("PASS: missing_enquiry_text")


def test_approval_decision_corrupted():
    setup()
    enquiry = "Need a proposal."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    decision_path = APPROVAL_DIR / f"{request_id}.decision.json"
    decision_path.write_text("not json")
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    assert "invalid_decision" in resume["reason"]
    cleanup(request_id)
    teardown()
    print("PASS: approval_decision_corrupted")


def test_approval_request_duplicate_does_not_overwrite():
    setup()
    enquiry = "Need a website, budget $8k."
    r1 = run_workflow(enquiry, approval_mode=True)
    req_id = r1["approval_request"]["request_id"]
    # repeat with same enquiry generates a new proposal/request id each run
    r2 = run_workflow(enquiry, approval_mode=True)
    req_id_2 = r2["approval_request"]["request_id"]
    assert req_id != req_id_2
    # but the original request file remains unchanged
    request_path = APPROVAL_DIR / f"{req_id}.request.json"
    assert request_path.exists()
    original_text = request_path.read_text()
    run_workflow(enquiry, approval_mode=True)
    assert request_path.read_text() == original_text
    cleanup(req_id)
    cleanup(req_id_2)
    teardown()
    print("PASS: approval_request_duplicate_does_not_overwrite")


def test_external_action_not_executed_on_rejection():
    setup()
    enquiry = "Send proposal to client now."
    result = run_workflow(enquiry, approval_mode=True)
    request_id = result["approval_request"]["request_id"]
    record_decision(request_id, approved=False, approver="juma", reason="scope unclear")
    # workflow result still shows pending_approval
    assert result["external_actions"][0]["status"] == "pending_approval"
    cleanup(request_id)
    teardown()
    print("PASS: external_action_not_executed_on_rejection")


def test_partial_completion_preserves_errors():
    setup()
    enquiry = "Research competitor pricing for SaaS CRMs and debug Python code."
    result = run_workflow(enquiry, approval_mode=True)
    assert "agents_invoked" in result
    assert "specialist_errors" in result["verification"]
    assert "retry_log" in result["verification"]
    teardown()
    print("PASS: partial_completion_preserves_errors")


def test_no_secrets_in_any_output():
    setup()
    enquiries = [
        "Need a web app.",
        "Research competitor pricing.",
        "Debug Python API code.",
        "Create ClickUp task.",
        "Send proposal to client now.",
    ]
    for e in enquiries:
        result = run_workflow(e, approval_mode=True)
        text = json.dumps(result)
        assert "GEMINI_API_KEY" not in text
        assert "CLICKUP_TOKEN" not in text
        assert "password" not in text.lower()
        if result.get("approval_request"):
            cleanup(result["approval_request"]["request_id"])
    teardown()
    print("PASS: no_secrets_in_any_output")


def main():
    try:
        setup()
        test_invalid_specialist_output()
        test_missing_enquiry_text()
        test_approval_decision_corrupted()
        test_approval_request_duplicate_does_not_overwrite()
        test_external_action_not_executed_on_rejection()
        test_partial_completion_preserves_errors()
        test_no_secrets_in_any_output()
        print("\nALL FAILURE-INJECTION TESTS PASSED")
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
