#!/usr/bin/env python3
"""
Unit tests for orchestrator.external_action

These tests focus on approval gating and action type dispatch with monkeypatched
HTTP and agent invocation, so they don't require OpenClaw or ClickUp.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Audit F-23, again: this suite wrote its ea-* fixtures into the repository's
# real .approval/ directory and executed the real send path, so running it
# messaged the operator. MUST precede the orchestrator imports.
import _state_isolation  # noqa: E402,F401

from orchestrator.external_action import execute_external_action  # noqa: E402
from orchestrator.approval import record_decision  # noqa: E402


def _request_dir() -> Path:
    # From the isolation module, never rebuilt from BASE. This function used to
    # return the repository's real .approval/ and the suite wrote fixtures into
    # it -- ea-* request files were found sitting beside live approvals.
    return _state_isolation.APPROVAL_DIR


def _clear(request_id: str):
    for suffix in [".request.json", ".decision.json"]:
        p = _request_dir() / f"{request_id}{suffix}"
        if p.exists():
            p.unlink()


def _write_request(request_id: str):
    _request_dir().mkdir(exist_ok=True)
    (_request_dir() / f"{request_id}.request.json").write_text(
        json.dumps({"request_id": request_id, "proposal": {"request_id": request_id}}, indent=2)
    )


def test_execute_blocks_when_not_approved():
    request_id = "ea-block-1"
    _clear(request_id)
    _write_request(request_id)
    result = execute_external_action({"request_id": request_id, "type": "send_proposal"})
    assert result["ok"] is False
    assert result["reason"] == "missing_decision"


def test_execute_send_proposal_with_approval():
    request_id = "ea-proposal-1"
    _clear(request_id)
    _write_request(request_id)
    record_decision(request_id, True, reason="approved")
    fake_post = {"ok": True, "status": 200}
    with patch("orchestrator.external_action._post", return_value=fake_post):
        result = execute_external_action(
            {"request_id": request_id, "type": "send_proposal"},
            proposal={"proposal_id": "p1", "body": "hello"},
        )
    assert result["ok"] is True
    assert result["verification"]["channel"] == "telegram"


def test_execute_create_clickup_task_via_projects_agent():
    request_id = "ea-clickup-1"
    _clear(request_id)
    _write_request(request_id)
    record_decision(request_id, True, reason="approved")
    projects_output = {"agent": "projects", "status": "ok", "id": "t1"}
    with patch("orchestrator.external_action._exec", return_value=(projects_output, None)):
        result = execute_external_action(
            {"request_id": request_id, "type": "create_clickup_task"},
            proposal={"payload": {"name": "Task", "description": "Desc"}},
        )
    assert result["ok"] is True
    assert result["verification"]["agent"] == "projects"


def test_execute_unsupported_type_returns_error():
    request_id = "ea-unsupported-1"
    _clear(request_id)
    _write_request(request_id)
    record_decision(request_id, True, reason="approved")
    result = execute_external_action({"request_id": request_id, "type": "unknown"})
    assert result["ok"] is False
    assert "unsupported_external_action_type" in result["reason"]
