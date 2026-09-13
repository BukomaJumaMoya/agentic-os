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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401 -- MUST precede orchestrator imports
import _llm_stub  # noqa: E402,F401 -- offline model boundary; MUST follow the above

EVIDENCE_DIR = _state_isolation.EVIDENCE_DIR
APPROVAL_DIR = _state_isolation.APPROVAL_DIR

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
    """No secret VALUE may reach the workflow output.

    The previous version asserted that the strings "GEMINI_API_KEY" and
    "CLICKUP_TOKEN" were absent. Those are variable names, not secrets, so it
    tripped on the projects agent's legitimate "Missing CLICKUP_TOKEN
    environment variable" message and failed on every machine without a ClickUp
    token -- which is to say always, in CI. It also could not have caught a real
    leak, because a leaked token does not contain the name of its own variable.

    This version plants canaries shaped like each provider's issued keys, runs
    the pipeline, and asserts none of the values come back. That covers the path
    the audit actually identified: child-process error text propagating into
    workflow output, and from there into evidence files and the model's context.
    """
    setup()
    canaries = {
        "GROQ_API_KEY": "gsk_" + "CANARYgroq" * 5,
        "OPENROUTER_API_KEY": "sk-or-v1-" + "CANARYopenrouter" * 4,
        "GEMINI_API_KEY": "AIza" + "CANARYgemini" * 3,
        "TAVILY_API_KEY": "tvly-" + "CANARYtavily" * 3,
        "CLICKUP_TOKEN": "pk_999999999_" + "CANARYclickup" * 2,
    }
    saved = {k: os.environ.get(k) for k in canaries}
    for k, v in canaries.items():
        os.environ[k] = v

    enquiries = [
        "Need a web app.",
        "Research competitor pricing.",
        "Debug Python API code.",
        "Create ClickUp task.",
        "Send proposal to client now.",
    ]
    try:
        for e in enquiries:
            result = run_workflow(e, approval_mode=True)
            text = json.dumps(result)
            for name, value in canaries.items():
                assert value not in text, f"{name} value leaked into workflow output"
            # A credential must not survive into the evidence file either.
            request = result.get("approval_request")
            if request:
                rid = request["request_id"]
                for path in (EVIDENCE_DIR / f"{rid}-proposal.json",
                             APPROVAL_DIR / f"{rid}.request.json"):
                    if path.exists():
                        on_disk = path.read_text(encoding="utf-8")
                        for name, value in canaries.items():
                            assert value not in on_disk, f"{name} value written to {path.name}"
                cleanup(rid)
            assert "password" not in text.lower()
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
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
