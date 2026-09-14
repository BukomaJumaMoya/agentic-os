#!/usr/bin/env python3
"""
Telegram Delivery for Approval Requests.

This module sends approval prompts to JUMA's Telegram via OpenClaw's
outbound channel, using the existing configured Telegram bot.

It requires OpenClaw to be running and reachable at OPENCLAW_GATEWAY_URL.
"""

import json
import os
import mimetypes
import uuid
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


# Structural block on outbound delivery.
#
# Set by tests/_no_outbound.py, which _state_isolation and conftest both import,
# and inherited by agent subprocesses because it lives in the environment.
#
# The check is here, inside the transport, rather than in each test. A suite
# that forgets to mock a sending function is refused by the transport; a suite
# that mocks the one sending function it knows about is not protected against
# the next one. Six proposal PDFs reached a real phone from a test sweep because
# document delivery was added after the suites had already mocked the prompt.
OUTBOUND_BLOCK_ENV = "AGENTIC_BLOCK_OUTBOUND"


def outbound_blocked() -> bool:
    """True when this process must not send anything to the outside world."""
    return str(os.getenv(OUTBOUND_BLOCK_ENV, "")).strip().lower() in ("1", "true", "yes")


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
    if outbound_blocked():
        return {"http_status": 0, "ok": False, "result": None,
                "error": f"outbound blocked: {OUTBOUND_BLOCK_ENV} is set"}
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


def _target_matches(target, user_id) -> bool:
    """Does this conversation belong to user_id?

    The gateway returns channel-qualified targets -- "telegram:1360833951" --
    while callers hold the bare id. A plain equality check never matched, so
    every send fell through to "no_conversation_found" despite the conversation
    being right there in the listing. It went unnoticed because the
    conversationRef cache answered first and the listing was rarely reached;
    once the cache was empty, every send failed.

    Accept either form, and compare only the id portion.
    """
    if target is None:
        return False
    text = str(target).strip()
    if not text:
        return False
    wanted = str(user_id).strip()
    # "telegram:123" -> "123"; a bare "123" is left alone.
    bare = text.split(":", 1)[1] if ":" in text else text
    return text == wanted or bare.strip() == wanted


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
        if not _target_matches(item.get("target"), user_id):
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


def send_direct_message(user_id: str, text: str) -> dict:
    """Deliver text to an allowlisted Telegram DM via conversations_send.

    The single outbound send path. Shares the conversationRef cache and the
    delivery_status vocabulary with send_approval_prompt, so a caller can never
    invent a fifth meaning for "sent".

    Returns {"sent", "delivery_status", "conversation_ref", "reason", "result"}.
    """
    if outbound_blocked():
        return {"sent": False, "delivery_status": "blocked",
                "conversation_ref": None,
                "reason": f"outbound blocked: {OUTBOUND_BLOCK_ENV} is set",
                "result": None}
    conversation_ref, problem = resolve_conversation_ref(user_id)
    if problem is not None:
        # Precondition failure, not a transport error. Do not retry.
        return {"sent": False, "delivery_status": "not_attempted",
                "conversation_ref": None, "reason": problem["reason"],
                "result": problem}

    invocation = _invoke_tool(
        "conversations_send",
        {"conversationRef": conversation_ref, "message": text},
    )
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
    return {"sent": delivery_status == "sent", "delivery_status": delivery_status,
            "conversation_ref": conversation_ref, "reason": reason,
            "result": invocation}


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

    delivery = send_direct_message(recipient, text)
    delivery_status = delivery["delivery_status"]
    reason = delivery["reason"]
    conversation_ref = delivery["conversation_ref"]
    result = delivery["result"]

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


# ---------------------------------------------------------------------------
# Document delivery.
#
# conversations_send CANNOT carry a file. Verified directly against the live
# gateway: three field shapes (attachments[{path,fileName}],
# attachments[{type,path}], media[{path}]) all returned status "sent" and
# delivered text only. The endpoint accepts unknown fields without complaint and
# silently drops them, so a "sent" there says nothing about an attachment. The
# gateway's own `attach` tool is agent-side and stays "Tool not available" at
# /tools/invoke even after being allowlisted.
#
# Documents therefore go straight to Telegram's Bot API sendDocument, which is
# confirmed working: it echoed back file_name, mime_type and a byte-exact
# file_size. That bypasses the gateway, so it gets its OWN status vocabulary
# rather than borrowing "sent/queued/suppressed/unknown" -- a different
# transport with different failure modes must not be described in words that
# imply the same guarantees.
# ---------------------------------------------------------------------------

CRLF = chr(13) + chr(10)
DOCUMENT_API = "https://api.telegram.org/bot{token}/sendDocument"
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # Telegram's own ceiling for bots.


def _bot_token() -> str:
    """Environment first; the OpenClaw config only as a fallback.

    Every other secret in this stack is read from the environment. Reading this
    one primarily from OpenClaw's config file would make it the single
    credential that behaves differently, and would couple proposal delivery to
    another tool's file layout. The fallback exists because that is where the
    token currently lives, not because it is the right source.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token and token.strip():
        return token.strip()
    try:
        config_path = Path(os.getenv("OPENCLAW_CONFIG")
                           or Path.home() / ".openclaw" / "openclaw.json")
        data = json.loads(config_path.read_text(encoding="utf-8"))
        token = ((data.get("channels") or {}).get("telegram") or {}).get("botToken")
    except Exception as e:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is not set and the OpenClaw config could not be "
            "read for a fallback: " + str(e))
    if not token or not str(token).strip():
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is not set and no channels.telegram.botToken "
            "was found in the OpenClaw config.")
    return str(token).strip()


def _multipart(fields: dict, file_field: str, filename: str, payload: bytes,
               content_type: str):
    """Build a multipart/form-data body. stdlib only; no new dependency."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        head = (
            "--" + boundary + CRLF
            + "Content-Disposition: form-data; name=" + chr(34) + key + chr(34) + CRLF
            + CRLF + str(value) + CRLF)
        parts.append(head.encode("utf-8"))
    head = (
        "--" + boundary + CRLF
        + "Content-Disposition: form-data; name=" + chr(34) + file_field + chr(34)
        + "; filename=" + chr(34) + filename + chr(34) + CRLF
        + "Content-Type: " + content_type + CRLF + CRLF)
    parts.append(head.encode("utf-8") + payload + CRLF.encode("utf-8"))
    parts.append(("--" + boundary + "--" + CRLF).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def send_document(user_id: str, path, caption: str = "") -> dict:
    """Deliver a file to an allowlisted Telegram DM via the Bot API.

    Returns {"delivered", "document_status", "reason", "file_name", "message_id"}.

    document_status is its own vocabulary, deliberately not the gateway's:
      delivered         Telegram confirmed the stored document object
      not_attempted     a precondition failed; nothing left this machine
      rejected          Telegram refused the request
      transport_failed  the request did not complete
    """
    out = {"delivered": False, "document_status": "not_attempted",
           "reason": None, "file_name": None, "message_id": None}

    path = Path(path)
    if not path.exists():
        out["reason"] = "file not found: " + path.name
        return out
    payload = path.read_bytes()
    if not payload:
        out["reason"] = "file is empty"
        return out
    if len(payload) > MAX_DOCUMENT_BYTES:
        out["reason"] = "file is %d bytes, above Telegram's limit" % len(payload)
        return out
    out["file_name"] = path.name

    try:
        token = _bot_token()
    except ConfigError as e:
        out["reason"] = str(e)
        return out

    # Last thing before the socket, and after the preconditions, so a blocked
    # process still exercises "file missing", "file empty" and "no token"
    # rather than short-circuiting them into one opaque refusal.
    if outbound_blocked():
        out["document_status"] = "blocked"
        out["reason"] = f"outbound blocked: {OUTBOUND_BLOCK_ENV} is set"
        return out

    body, content_type = _multipart(
        {"chat_id": str(user_id), "caption": (caption or "")[:1024]},
        "document", path.name, payload,
        mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    req = urllib.request.Request(
        DOCUMENT_API.format(token=token), data=body, method="POST",
        headers={"Content-Type": content_type,
                 "User-Agent": "juma-freelance-ai/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        # Never echo the request URL anywhere: it carries the bot token.
        out["document_status"] = "rejected"
        out["reason"] = "Telegram refused the document (HTTP %s): %s" % (e.code, detail)
        return out
    except Exception as e:
        out["document_status"] = "transport_failed"
        out["reason"] = type(e).__name__ + ": " + str(e)
        return out

    if not parsed.get("ok"):
        out["document_status"] = "rejected"
        out["reason"] = str(parsed.get("description"))[:200]
        return out

    result = parsed.get("result") or {}
    document = result.get("document") or {}
    out.update({
        "delivered": True,
        "document_status": "delivered",
        "message_id": result.get("message_id"),
        # Telegram's own echo of what it stored. This is the only real proof
        # that a document -- not a link, not text -- was delivered.
        "file_name": document.get("file_name") or path.name,
        "mime_type": document.get("mime_type"),
        "file_size": document.get("file_size"),
    })
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
