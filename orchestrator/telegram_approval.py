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

# Repo root ahead of this file's own directory, before the first orchestrator
# import -- see the note in telegram_commands.py. orchestrator/orchestrator.py
# shadows the package when this module runs as a script, and a try/except cannot
# recover because the failed import poisons sys.modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import approval as _approval  # noqa: E402

ConfigError = _approval.ConfigError

# The gateway has no REST message route. Outbound delivery goes through the
# tool-invocation endpoint, addressed by an opaque conversationRef rather than
# by channel+chat-id. Both tool schemas are additionalProperties:false, so the
# args below must contain exactly the documented fields and nothing else.
TOOLS_INVOKE_PATH = "/tools/invoke"

# conversations_send returns one of four delivery statuses. Only "sent" is a
# confirmed delivery; the other three are distinct non-deliveries and are kept
# verbatim rather than collapsed into a generic failure.
_NOT_DELIVERED_REASON = {
    "queued": "queued by the gateway; not confirmed delivered",
    "suppressed": "suppressed by the gateway; not delivered",
    "unknown": "gateway reported delivery status 'unknown'; not confirmed delivered",
}

_CONVERSATION_CACHE_PATH = _approval.APPROVAL_DIR / ".conversation-cache.json"
_conversation_ref_memo = {}


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


def _unwrap_tool_result(result):
    """Unwrap the tool layer's jsonResult() envelope to the payload dict.

    The real shape, observed live from conversations_send, is
    {content: [{type: "text", text: "<json>"}], details: {...payload}} -- the
    payload lives under "details". The remaining keys are defensive fallbacks
    for tool envelopes not yet observed. Returns the value unchanged once it
    already looks like the payload.
    """
    for _ in range(3):
        if not isinstance(result, dict) or "conversations" in result or "status" in result:
            break
        for key in ("details", "json", "result", "value", "data"):
            inner = result.get(key)
            if isinstance(inner, (dict, list)):
                result = inner
                break
        else:
            break
    return result


def _invoke_tool(name: str, args: dict, timeout: int = 30) -> dict:
    """POST one tool invocation to /tools/invoke.

    Returns a normalised envelope {http_status, ok, result, error} so callers
    can tell transport failure, gateway rejection and tool outcome apart.
    """
    headers = {"Content-Type": "application/json"}
    try:
        headers["Authorization"] = f"Bearer {_approval.gateway_token()}"
    except ConfigError as e:
        return {"http_status": 0, "ok": False, "result": None,
                "error": f"missing gateway token: {e}"}
    body = json.dumps({"name": name, "args": args}).encode("utf-8")
    req = urllib.request.Request(
        f"{_gateway_url()}{TOOLS_INVOKE_PATH}", data=body, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(resp.status)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            raw = ""
        return {"http_status": int(e.code), "ok": False, "result": None,
                "error": raw or f"HTTP {e.code}"}
    except Exception as e:
        return {"http_status": 0, "ok": False, "result": None, "error": str(e)}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"http_status": status, "ok": False, "result": None,
                "error": f"non-JSON response: {raw[:300]}"}
    if not parsed.get("ok"):
        return {"http_status": status, "ok": False, "result": None,
                "error": json.dumps(parsed.get("error") or parsed)[:500]}
    return {"http_status": status, "ok": True,
            "result": _unwrap_tool_result(parsed.get("result")), "error": None}


def _load_cached_ref(user_id: str):
    if user_id in _conversation_ref_memo:
        return _conversation_ref_memo[user_id]
    try:
        cache = json.loads(_CONVERSATION_CACHE_PATH.read_text())
    except Exception:
        return None
    ref = (cache or {}).get(user_id) if isinstance(cache, dict) else None
    if isinstance(ref, str) and ref:
        _conversation_ref_memo[user_id] = ref
        return ref
    return None


def _store_cached_ref(user_id: str, ref: str) -> None:
    _conversation_ref_memo[user_id] = ref
    try:
        _approval.APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            cache = json.loads(_CONVERSATION_CACHE_PATH.read_text())
        except Exception:
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        cache[user_id] = ref
        _CONVERSATION_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass  # the cache is an optimisation; never fail a send over it


def resolve_conversation_ref(user_id: str, use_cache: bool = True):
    """Return (conversationRef, problem) for the allowlisted Telegram DM.

    problem is None on success. "no_conversation_found" is a precondition, not
    a transport failure: the gateway only knows a conversation once it has seen
    one, and there is no API to create one. Callers must not retry it.
    """
    if use_cache:
        cached = _load_cached_ref(str(user_id))
        if cached:
            return cached, None
    listed = _invoke_tool("conversations_list", {"channel": "telegram"})
    if not listed["ok"]:
        return None, {"reason": "conversations_list_failed",
                      "detail": listed["error"], "http_status": listed["http_status"]}
    payload = listed["result"]
    conversations = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(conversations, list):
        return None, {"reason": "conversations_list_unexpected_shape",
                      "detail": str(payload)[:300], "http_status": listed["http_status"]}
    for item in conversations:
        if not isinstance(item, dict) or item.get("kind") != "direct":
            continue
        if str(item.get("target", "")).strip() != str(user_id):
            continue
        ref = item.get("conversationRef")
        if isinstance(ref, str) and ref:
            _store_cached_ref(str(user_id), ref)
            return ref, None
    return None, {"reason": "no_conversation_found",
                  "detail": (f"no kind='direct' telegram conversation with target={user_id}; "
                             "the gateway only learns a conversation after seeing it"),
                  "http_status": listed["http_status"],
                  "conversations_seen": len(conversations)}


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
        f"/apr_approve {request_id}",
        f"/apr_reject {request_id}",
    ]
    text = "\n".join(message_lines)

    try:
        recipient = _allowed_user_id()
    except ConfigError as e:
        return {"sent": False, "channel": "telegram", "request_id": request_id,
                "delivery_status": "not_attempted",
                "reason": f"allowlist not configured: {e}"}

    conversation_ref, problem = resolve_conversation_ref(recipient)
    if problem is not None:
        # Precondition failure, not a transport error. Do not retry.
        result = problem
        delivery_status = "not_attempted"
        reason = problem["reason"]
        conversation_ref = None
    else:
        invocation = _invoke_tool(
            "conversations_send",
            {"conversationRef": conversation_ref, "message": text},
        )
        result = invocation
        status = invocation["result"].get("status") if (
            invocation["ok"] and isinstance(invocation["result"], dict)) else None
        if not invocation["ok"]:
            delivery_status = "invoke_failed"
            reason = invocation["error"] or f"HTTP {invocation['http_status']}"
        elif status == "sent":
            delivery_status = "sent"
            reason = None
        elif status in _NOT_DELIVERED_REASON:
            delivery_status = status  # queued | suppressed | unknown, verbatim
            reason = _NOT_DELIVERED_REASON[status]
        else:
            delivery_status = "unrecognised_status"
            reason = f"conversations_send returned status={status!r}"

    sent = delivery_status == "sent"
    state = delivery_status
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
        out = {"sent": True, "channel": "telegram", "request_id": request_id,
               "delivery_status": delivery_status,
               "conversation_ref": conversation_ref, "result": result}
    else:
        # Every non-delivery keeps its own distinguishable delivery_status;
        # queued / suppressed / unknown are never collapsed into one string.
        out = {
            "sent": False,
            "channel": "telegram",
            "request_id": request_id,
            "delivery_status": delivery_status,
            "conversation_ref": conversation_ref,
            "reason": reason,
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
