#!/usr/bin/env python3
"""
Telegram command parser and action for OpenClaw inbound messages.

This module is intended to be invoked by OpenClaw when it receives a
Telegram message from JUMA. It parses approval commands and updates
the approval decision files so the flagship workflow can resume.

Supported commands:
- APPROVE <request_id> [reason...]
- REJECT <request_id> [reason...]
- STATUS <request_id>
- LIST
- RESUME <request_id>

Security:
- Only allowlist user ID 1360833951 may approve/reject.
- Commands are case-insensitive.
- Returns structured JSON suitable for OpenClaw tool output.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ALLOWED_USER_ID = os.getenv("OPENCLAW_ALLOWED_USER_ID", "1360833951")
BASE = Path(__file__).resolve().parent.parent
APPROVAL_DIR = BASE / ".approval"


def _resolve_request_dir() -> Path:
    return APPROVAL_DIR


def _decision_path(request_id: str) -> Path:
    return _resolve_request_dir() / f"{request_id}.decision.json"


def _request_path(request_id: str) -> Path:
    return _resolve_request_dir() / f"{request_id}.request.json"


def _list_pending():
    pending = []
    if not _resolve_request_dir().exists():
        return pending
    for req in sorted(_resolve_request_dir().glob("*.request.json")):
        try:
            rec = json.loads(req.read_text())
        except Exception:
            continue
        rid = rec.get("request_id")
        if not rid:
            continue
        if not _decision_path(rid).exists():
            pending.append({
                "request_id": rid,
                "status": "pending",
                "timestamp": rec.get("timestamp"),
                "proposal": rec.get("proposal"),
            })
            continue
        try:
            decision = json.loads(_decision_path(rid).read_text())
        except Exception:
            continue
        pending.append({
            "request_id": rid,
            "status": "approved" if decision.get("approved") else "rejected",
            "timestamp": decision.get("timestamp"),
            "reason": decision.get("reason"),
            "proposal": rec.get("proposal"),
        })
    return pending


def handle_telegram_command(user_id: str, text: str) -> dict:
    if user_id != ALLOWED_USER_ID:
        return {"ok": False, "error": "not_allowed", "allowed_user": ALLOWED_USER_ID}

    trimmed = text.strip()
    if not trimmed:
        return {"ok": False, "error": "empty_command"}

    parts = trimmed.split(maxsplit=2)
    command = parts[0].upper()
    request_id = parts[1] if len(parts) > 1 else None
    rest = parts[2] if len(parts) > 2 else ""

    if command == "APPROVE":
        if not request_id:
            return {"ok": False, "error": "missing_request_id", "example": "APPROVE <request_id>"}
        decision_path = _decision_path(request_id)
        if not decision_path.exists():
            return {"ok": False, "error": "request_not_found", "request_id": request_id}
        try:
            existing = json.loads(decision_path.read_text())
            if existing.get("approved") is True:
                return {"ok": True, "status": "already_approved", "request_id": request_id, "decision": existing}
        except Exception:
            pass
        decision = {
            "request_id": request_id,
            "approved": True,
            "approver": "telegram-user",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": rest or "approved",
            "channel": "telegram",
        }
        decision_path.write_text(json.dumps(decision, indent=2))
        return {"ok": True, "status": "approved", "request_id": request_id, "decision": decision}

    if command == "REJECT":
        if not request_id:
            return {"ok": False, "error": "missing_request_id", "example": "REJECT <request_id>"}
        decision_path = _decision_path(request_id)
        if not decision_path.exists():
            return {"ok": False, "error": "request_not_found", "request_id": request_id}
        try:
            existing = json.loads(decision_path.read_text())
            if existing.get("approved") is True:
                return {"ok": True, "status": "already_decided", "request_id": request_id, "decision": existing}
        except Exception:
            pass
        decision = {
            "request_id": request_id,
            "approved": False,
            "approver": "telegram-user",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": rest or "rejected",
            "channel": "telegram",
        }
        decision_path.write_text(json.dumps(decision, indent=2))
        return {"ok": True, "status": "rejected", "request_id": request_id, "decision": decision}

    if command == "STATUS":
        if not request_id:
            return {"ok": False, "error": "missing_request_id", "example": "STATUS <request_id>"}
        req_path = _request_path(request_id)
        dec_path = _decision_path(request_id)
        if not req_path.exists():
            return {"ok": False, "error": "request_not_found", "request_id": request_id}
        rec = json.loads(req_path.read_text())
        if not dec_path.exists():
            return {"ok": True, "status": "pending", "request_id": request_id, "proposal": rec.get("proposal")}
        decision = json.loads(dec_path.read_text())
        return {"ok": True, "status": "approved" if decision.get("approved") else "rejected", "request_id": request_id, "decision": decision, "proposal": rec.get("proposal")}

    if command == "LIST":
        return {"ok": True, "pending": _list_pending()}

    if command == "RESUME":
        if not request_id:
            return {"ok": False, "error": "missing_request_id", "example": "RESUME <request_id>"}
        from orchestrator.approval import resume_if_approved
        resume = resume_if_approved(request_id)
        if resume.get("approved"):
            return {"ok": True, "status": "ready", "request_id": request_id, "resume": resume}
        return {"ok": True, "status": "not_ready", "request_id": request_id, "resume": resume}

    return {"ok": False, "error": "unknown_command", "supported": ["APPROVE", "REJECT", "STATUS", "LIST", "RESUME"]}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    user_id = data.get("user_id") or os.getenv("OPENCLAW_ALLOWED_USER_ID", "1360833951")
    text = data.get("text") or data.get("message") or ""
    result = handle_telegram_command(user_id, text)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
