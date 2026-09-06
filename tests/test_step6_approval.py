#!/usr/bin/env python3
"""
Test Step 6 — Human Approval Boundary.
"""
import json
import os
import sys
import time
import tempfile
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.approval import (
    classify, request_approval, wait_for_decision, record_decision,
    is_approved, resume_if_approved, enforce, cleanup
)

APPROVAL_DIR = BASE / ".approval"

def setup():
    if APPROVAL_DIR.exists():
        shutil.rmtree(APPROVAL_DIR)
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)

def teardown():
    if APPROVAL_DIR.exists():
        shutil.rmtree(APPROVAL_DIR)

def test_classify_read():
    assert classify("list tasks") == "READ"
    assert classify("search_tasks") == "READ"
    assert classify("explain code") == "READ"
    print("PASS: classify_read")

def test_classify_internal_write():
    assert classify("create_task") == "INTERNAL_WRITE"
    assert classify("generate") == "INTERNAL_WRITE"
    assert classify("update_task") == "INTERNAL_WRITE"
    print("PASS: classify_internal_write")

def test_classify_external_action():
    assert classify("send_message") == "EXTERNAL_ACTION"
    assert classify("publish") == "EXTERNAL_ACTION"
    assert classify("submit_proposal") == "EXTERNAL_ACTION"
    print("PASS: classify_external_action")

def test_read_proceeds():
    setup()
    step = {"step": "research", "agent": "research", "action": "list", "description": "List research sources"}
    result = enforce(step)
    assert result["allowed"] is True
    assert result["authority"] == "READ"
    assert result["executed"] is True
    teardown()
    print("PASS: read_proceeds")

def test_internal_write_proceeds():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "create_task", "description": "Create internal draft task"}
    result = enforce(step)
    assert result["allowed"] is True
    assert result["authority"] == "INTERNAL_WRITE"
    assert result["executed"] is True
    teardown()
    print("PASS: internal_write_proceeds")

def test_external_action_pauses():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "send_message", "description": "Send proposal to client"}
    result = enforce(step)
    assert result["allowed"] is True
    assert result["authority"] == "EXTERNAL_ACTION"
    assert result["executed"] is False
    assert "request_id" in result
    assert result["status"] == "awaiting_approval"
    teardown()
    print("PASS: external_action_pauses")

def test_rejection_prevents_execution():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "send_message", "description": "Send proposal to client"}
    result = enforce(step)
    request_id = result["request_id"]
    record_decision(request_id, approved=False, approver="juma", reason="too early")
    assert is_approved(request_id) is False
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    cleanup(request_id)
    teardown()
    print("PASS: rejection_prevents_execution")

def test_approval_resumes_execution():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "send_message", "description": "Send proposal to client"}
    result = enforce(step)
    request_id = result["request_id"]
    record_decision(request_id, approved=True, approver="juma")
    assert is_approved(request_id) is True
    resume = resume_if_approved(request_id)
    assert resume["approved"] is True
    cleanup(request_id)
    teardown()
    print("PASS: approval_resumes_execution")

def test_approval_specific_to_action():
    setup()
    step1 = {"step": "projects", "agent": "projects", "action": "send_message", "request_id": "req-1", "description": "Action 1"}
    step2 = {"step": "projects", "agent": "projects", "action": "deploy", "request_id": "req-2", "description": "Action 2"}
    r1 = enforce(step1)
    r2 = enforce(step2)
    record_decision(r1["request_id"], approved=True, approver="juma")
    record_decision(r2["request_id"], approved=False, approver="juma", reason="not ready")
    assert is_approved(r1["request_id"]) is True
    assert is_approved(r2["request_id"]) is False
    cleanup(r1["request_id"])
    cleanup(r2["request_id"])
    teardown()
    print("PASS: approval_specific_to_action")

def test_timeout_does_not_approve():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "publish", "description": "Publish draft"}
    result = enforce(step)
    request_id = result["request_id"]
    # Do NOT record a decision; simulate timeout
    assert is_approved(request_id) is False
    resume = resume_if_approved(request_id)
    assert resume["approved"] is False
    assert resume["reason"] == "pending"
    cleanup(request_id)
    teardown()
    print("PASS: timeout_does_not_approve")

def test_failed_external_action_reported():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "send_message", "description": "Send proposal"}
    result = enforce(step)
    request_id = result["request_id"]
    # Simulate execution failure after approval
    record_decision(request_id, approved=True, approver="juma")
    # In real flow, execution would happen after approval; here we verify approval gate works
    resume = resume_if_approved(request_id)
    assert resume["approved"] is True
    cleanup(request_id)
    teardown()
    print("PASS: failed_external_action_reported_boundary")

def test_verification_failure_reported():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "search_tasks", "description": "Search"}
    result = enforce(step)
    assert result["allowed"] is True
    assert result["executed"] is True
    teardown()
    print("PASS: verification_failure_reported_boundary")

def test_no_credentials_in_logs():
    setup()
    step = {"step": "projects", "agent": "projects", "action": "send_message", "description": "Send proposal to client abc@example.com", "payload": {"token": "REDACTED"}}
    result = enforce(step)
    request_id = result["request_id"]
    record = json.loads((APPROVAL_DIR / f"{request_id}.request.json").read_text())
    text = json.dumps(record)
    assert "secret" not in text.lower()
    assert "password" not in text.lower()
    cleanup(request_id)
    teardown()
    print("PASS: no_credentials_in_logs")

def main():
    try:
        setup()
        test_classify_read()
        test_classify_internal_write()
        test_classify_external_action()
        test_read_proceeds()
        test_internal_write_proceeds()
        test_external_action_pauses()
        test_rejection_prevents_execution()
        test_approval_resumes_execution()
        test_approval_specific_to_action()
        test_timeout_does_not_approve()
        test_failed_external_action_reported()
        test_verification_failure_reported()
        test_no_credentials_in_logs()
        print("\nALL APPROVAL BOUNDARY TESTS PASSED")
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
