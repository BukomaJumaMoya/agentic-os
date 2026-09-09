#!/usr/bin/env python3
"""
Telegram Delivery for Approval Requests.

This module sends approval prompts to JUMA's Telegram via OpenClaw's
outbound channel, using the existing configured Telegram bot.

It requires OpenClaw to be running and reachable at OPENCLAW_GATEWAY_URL.
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

try:
    from orchestrator import approval as _approval
except ImportError:  # imported bare, with orchestrator/ on sys.path (tests)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator import approval as _approval

ConfigError = _approval.ConfigError


def _gateway_url() -> str:
    return _approval.gateway_url()


def _allowed_user_id() -> str:
    return _approval.allowed_user_id()


def _resolve_request_dir() -> Path:
    return _approval.APPROVAL_DIR


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    url = f"{_gateway_url()}{path}"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        # openclaw.json runs gateway.auth.mode="token"; an unauthenticated POST
        # is rejected anyway, and sending one silently hides the misconfig.
        headers["Authorization"] = f"Bearer {_approval.gateway_token()}"
    except ConfigError as e:
        return {"ok": False, "status": 0, "error": f"missing gateway token: {e}"}
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
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

    try:
        recipient = _allowed_user_id()
    except ConfigError as e:
        return {"sent": False, "channel": "telegram", "request_id": request_id,
                "reason": f"allowlist not configured: {e}"}

    payload = {
        "to": recipient,
        "text": text,
        "approval_request_id": request_id,
    }

    result = _post("/message/send", payload)

    sent = bool(result.get("ok")) or int(result.get("status", 0)) == 200
    state = "sent" if sent else "failed"
    state_write_error = None
    try:
        if request_path.exists():
            rec = json.loads(request_path.read_text())
        else:
            rec = {}
        rec["telegram_prompt_state"] = state
        rec["telegram_prompt_sent_at"] = datetime.now(timezone.utc).isoformat()
        rec["telegram_prompt_result"] = result
        request_path.write_text(json.dumps(rec, indent=2))
    except Exception as e:
        # Never silent: the caller decides what an unrecorded prompt means.
        state_write_error = str(e)

    if sent:
        out = {"sent": True, "channel": "telegram", "request_id": request_id, "result": result}
    else:
        # `result.get("error") or result.get("error")` was a typo: a non-2xx
        # response with no error body yielded reason=None, which reads as
        # "no problem". Always give a concrete reason.
        out = {
            "sent": False,
            "channel": "telegram",
            "request_id": request_id,
            "reason": result.get("error") or f"gateway returned status {result.get('status')}",
            "result": result,
        }
    if state_write_error:
        out["state_write_error"] = state_write_error
    return out


def record_telegram_decision(request_id: str, approved: bool, approver: str = "telegram-user", reason: str = None) -> dict:
    """Thin wrapper: orchestrator.approval owns decision records."""
    return _approval.record_decision(
        request_id, approved, approver=approver, reason=reason, channel="telegram"
    )


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
