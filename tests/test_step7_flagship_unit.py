#!/usr/bin/env python3
"""
Unit tests for Step 7 — Flagship End-to-End Workflow.

No external side effects. Fast. Isolated.
"""
import json
import sys
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

EVIDENCE_DIR = BASE / "evidence"
APPROVAL_DIR = BASE / ".approval"

from orchestrator.flagship import classify_enquiry, load_config, synthesize_proposal, request_approval
from orchestrator.approval import record_decision, is_approved, cleanup


def setup():
    for d in [EVIDENCE_DIR, APPROVAL_DIR]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def teardown():
    for d in [EVIDENCE_DIR, APPROVAL_DIR]:
        if d.exists():
            shutil.rmtree(d)


def test_classify_enquiry_research():
    result = classify_enquiry("Research competitor pricing for SaaS CRMs.")
    assert "research" in result["selected_agents"]
    teardown()
    print("PASS: classify_enquiry_research")


def test_classify_enquiry_coding():
    result = classify_enquiry("Debug our Python API integration code.")
    assert "coding" in result["selected_agents"]
    teardown()
    print("PASS: classify_enquiry_coding")


def test_classify_enquiry_projects():
    result = classify_enquiry("Create a ClickUp task for onboarding.")
    assert "projects" in result["selected_agents"]
    teardown()
    print("PASS: classify_enquiry_projects")


def test_classify_enquiry_missing_info():
    result = classify_enquiry("We need an AI chatbot.")
    assert "budget" in result["missing_information"]
    teardown()
    print("PASS: classify_enquiry_missing_info")


def test_load_config_exists():
    config = load_config()
    assert isinstance(config, dict)
    teardown()
    print("PASS: load_config_exists")


def test_synthesize_proposal_uses_config():
    config = load_config()
    proposal = synthesize_proposal("Need a web app", {}, {}, {}, config)
    assert proposal["proposal_id"]
    assert proposal["body"]
    assert config.get("name") in proposal["body"]
    teardown()
    print("PASS: synthesize_proposal_uses_config")


def test_synthesize_proposal_without_config():
    proposal = synthesize_proposal("Need a web app", {}, {}, {}, {})
    assert proposal["proposal_id"]
    assert proposal["body"]
    teardown()
    print("PASS: synthesize_proposal_without_config")


def test_request_approval_idempotent():
    proposal = {"proposal_id": "req-1", "body": "test"}
    r1 = request_approval(proposal, "enquiry")
    r2 = request_approval(proposal, "enquiry")
    request_path = APPROVAL_DIR / "req-1.request.json"
    evidence_path = EVIDENCE_DIR / "req-1-proposal.json"
    mtime1 = request_path.stat().st_mtime
    mtime2 = request_path.stat().st_mtime
    assert mtime1 == mtime2
    cleanup("req-1")
    teardown()
    print("PASS: request_approval_idempotent")


def test_record_decision_not_overwritten_by_default():
    request_id = "req-2"
    request_approval({"proposal_id": request_id}, "enquiry")
    record_decision(request_id, approved=True, approver="juma", reason="approved first")
    record_decision(request_id, approved=False, approver="other", reason="changed")
    decision = json.loads((APPROVAL_DIR / f"{request_id}.decision.json").read_text())
    assert decision["approved"] is True
    assert decision["reason"] == "approved first"
    cleanup(request_id)
    teardown()
    print("PASS: record_decision_not_overwritten_by_default")


def test_cleanup_removes_artifacts():
    request_id = "req-3"
    request_approval({"proposal_id": request_id}, "enquiry")
    record_decision(request_id, approved=True, approver="juma")
    cleanup(request_id)
    assert not (APPROVAL_DIR / f"{request_id}.request.json").exists()
    assert not (APPROVAL_DIR / f"{request_id}.decision.json").exists()
    teardown()
    print("PASS: cleanup_removes_artifacts")


def main():
    try:
        setup()
        test_classify_enquiry_research()
        test_classify_enquiry_coding()
        test_classify_enquiry_projects()
        test_classify_enquiry_missing_info()
        test_load_config_exists()
        test_synthesize_proposal_uses_config()
        test_synthesize_proposal_without_config()
        test_request_approval_idempotent()
        test_record_decision_not_overwritten_by_default()
        test_cleanup_removes_artifacts()
        print("\nALL UNIT TESTS PASSED")
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
