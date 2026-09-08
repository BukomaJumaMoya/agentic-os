#!/usr/bin/env python3
"""
Unit tests for orchestrator.telegram_approval

These tests exercise only filesystem-backed behavior and HTTP transport
through a monkeypatched `_post`, so they don't require OpenClaw or Telegram.
"""

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "orchestrator"))

from telegram_approval import send_approval_prompt, record_telegram_decision, _resolve_request_dir  # noqa: E402


def _write_request(request_id: str, proposal: dict = None, enquiry: str = "test enquiry"):
    request_dir = _resolve_request_dir()
    request_dir.mkdir(exist_ok=True)
    rec = {
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "enquiry": enquiry,
        "proposal": proposal or {"request_id": request_id, "action": "send_proposal", "description": "desc", "task": "task"},
        "status": "pending",
    }
    (request_dir / f"{request_id}.request.json").write_text(json.dumps(rec, indent=2))


def _clear_decision(request_id: str):
    p = _resolve_request_dir() / f"{request_id}.decision.json"
    if p.exists():
        p.unlink()


def test_send_approval_prompt_success():
    request_id = "test-approval-1"
    _write_request(request_id)
    _clear_decision(request_id)
    with patch("telegram_approval._post", return_value={"ok": True, "status": 200}):
        result = send_approval_prompt(request_id, {"action": "send_proposal", "description": "desc", "task": "task"})
    assert result["sent"] is True
    assert result["channel"] == "telegram"
    rec = json.loads((_resolve_request_dir() / f"{request_id}.request.json").read_text())
    assert rec["telegram_prompt_state"] == "sent"
    assert rec["telegram_prompt_sent_at"]


def test_send_approval_prompt_idempotent():
    request_id = "test-approval-2"
    _write_request(request_id)
    request_path = _resolve_request_dir() / f"{request_id}.request.json"
    rec = json.loads(request_path.read_text())
    rec["telegram_prompt_state"] = "sent"
    request_path.write_text(json.dumps(rec, indent=2))
    with patch("telegram_approval._post", return_value={"ok": False, "status": 500}):
        result = send_approval_prompt(request_id, {"action": "send_proposal"})
    assert result["sent"] is True
    assert result["status"] == "already_sent"


def test_send_approval_prompt_failure():
    request_id = "test-approval-3"
    _write_request(request_id)
    with patch("orchestrator.telegram_approval._post", return_value={"ok": False, "status": 500, "error": "boom"}):
        result = send_approval_prompt(request_id, {"action": "send_proposal"})
    assert result["sent"] is False
    assert result["channel"] == "telegram"
    rec = json.loads((_resolve_request_dir() / f"{request_id}.request.json").read_text())
    assert rec["telegram_prompt_state"] == "failed"


def test_record_telegram_decision_writes_decision():
    request_id = "test-approval-4"
    _clear_decision(request_id)
    decision = record_telegram_decision(request_id, True, approver="telegram-user", reason="approved")
    assert decision["approved"] is True
    assert decision["channel"] == "telegram"
    stored = json.loads((_resolve_request_dir() / f"{request_id}.decision.json").read_text())
    assert stored["approved"] is True


def test_record_telegram_decision_does_not_overwrite_true():
    request_id = "test-approval-5"
    _clear_decision(request_id)
    record_telegram_decision(request_id, True)
    decision = record_telegram_decision(request_id, False, reason="late")
    assert decision["approved"] is True
