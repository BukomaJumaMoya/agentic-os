#!/usr/bin/env python3
"""
Telegram Delivery for Approval Requests.

This module sends approval prompts to JUMA's Telegram via OpenClaw's
outbound channel, using the existing configured Telegram bot.

It requires OpenClaw to be running and reachable at OPENCLAW_GATEWAY_URL.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"
DEFAULT_ALLOWED_USER_ID = "1360833951"


def _gateway_url() -> str:
    return os.getenv("OPENCLAW_GATEWAY_URL", DEFAULT_GATEWAY_URL)


def _allowed_user_id() -> str:
    return os.getenv("OPENCLAW_ALLOWED_USER_ID", DEFAULT_ALLOWED_USER_ID)


def _resolve_request_dir() -> Path:
    return Path(__file__).resolve().parent.parent / ".approval"


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    url = f"{_gateway_url()}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return json.loads(resp.read().decode("utf-8"))
            except Exception:
                return {"ok": True, "status": int(resp.status)}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        return {"ok": False, "status": int(e.code), "error": body}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def send_approval_prompt(request_id: str, proposal: dict) -> dict:
    """
    Send an approval prompt to JUMA via OpenClaw Telegram channel.

    Returns:
      - {"sent": True, "channel": "telegram", ...}
      - {"sent": False, "reason": "...", "channel": "telegram"}
    """
    request_dir = _resolve_request_dir()
    request_path = request_dir / f"{request_id}.request.json"
    decision_path = request_dir / f"{request_id}.decision.json"

    if request_path.exists():
        try:
            existing = json.loads(request_path.read_text())
            prompt_state = existing.get("telegram_prompt_state")
            if prompt_state == "sent":
                return {
                    "sent": True,
                    "channel": "telegram",
                    "request_id": request_id,
                    "status": "already_sent",
                }
        except Exception:
            pass

    enquiry = ""
    try:
        record = json.loads(request_path.read_text())
        enquiry = record.get("enquiry", "")
    except Exception:
        pass

    action = proposal.get("action", "external_action")
    description = proposal.get("description", "Approve or reject this action")
    task = proposal.get("task", "")

    message_lines = [
        "🛡️ Approval required",
        "",
        f"Request: {request_id}",
        f"Action: {action}",
        f"Description: {description}",
    ]
    if task:
        message_lines.append(f"Task: {task}")
    message_lines += [
        "",
        "Reply with:",
        f"APPROVE {request_id}",
        f"REJECT {request_id}",
    ]
    text = "\n".join(message_lines)

    payload = {
        "to": _allowed_user_id(),
        "text": text,
        "approval_request_id": request_id,
    }

    result = _post("/message/send", payload)

    sent = bool(result.get("ok")) or int(result.get("status", 0)) == 200
    state = "sent" if sent else "failed"
    try:
        if request_path.exists():
            rec = json.loads(request_path.read_text())
        else:
            rec = {}
        rec["telegram_prompt_state"] = state
        rec["telegram_prompt_sent_at"] = datetime.now(timezone.utc).isoformat()
        rec["telegram_prompt_result"] = result
        request_path.write_text(json.dumps(rec, indent=2))
    except Exception:
        pass

    if sent:
        return {"sent": True, "channel": "telegram", "request_id": request_id, "result": result}
    return {"sent": False, "channel": "telegram", "request_id": request_id, "reason": result.get("error") or result.get("error"), "result": result}


def record_telegram_decision(request_id: str, approved: bool, approver: str = "telegram-user", reason: str = None) -> dict:
    decision_path = _resolve_request_dir() / f"{request_id}.decision.json"
    decision = {
        "request_id": request_id,
        "approved": approved,
        "approver": approver,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason or ("approved" if approved else "rejected"),
        "channel": "telegram",
    }
    if decision_path.exists():
        try:
            existing = json.loads(decision_path.read_text())
            if existing.get("approved") is True:
                return existing
        except Exception:
            pass
    decision_path.write_text(json.dumps(decision, indent=2))
    return decision


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    request_id = data.get("request_id")
    if not request_id:
        print(json.dumps({"error": "Missing request_id"}))
        sys.exit(1)

    proposal = data.get("proposal") or {}
    if not proposal:
        request_dir = _resolve_request_dir()
        request_path = request_dir / f"{request_id}.request.json"
        if request_path.exists():
            try:
                proposal = json.loads(request_path.read_text()).get("proposal", {})
            except Exception:
                pass

    result = send_approval_prompt(request_id, proposal)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("sent") else 2)


if __name__ == "__main__":
    main()
