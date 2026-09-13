#!/usr/bin/env python3
"""
Human Approval Boundary — explicit enforcement between internal work and
external actions.

Authority levels:
- READ
- INTERNAL_WRITE
- EXTERNAL_ACTION
"""

import json
import os
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent
# Single knob for relocating runtime state out of the repo tree. Defaults to
# the current in-repo location so existing callers and tests are unaffected.
STATE_ROOT = Path(os.getenv("AGENTIC_STATE_DIR") or BASE).resolve()
APPROVAL_DIR = STATE_ROOT / ".approval"
EVIDENCE_DIR = STATE_ROOT / "evidence"
CONFIG_PATH = BASE / "config" / "juma.json"


class ConfigError(RuntimeError):
    """Required operator configuration is missing."""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        # Explicit: the file is UTF-8, but read_text() defaults to the locale
        # encoding (cp1252 on Windows), which mojibakes the en dash in
        # default_rate into every config_used block and evidence record.
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _config_value(env_var: str, *config_path: str):
    """Environment first, then a dotted path in config/juma.json."""
    v = os.getenv(env_var)
    if v and v.strip():
        return v.strip()
    node = load_config()
    for key in config_path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, (str, int)) and str(node).strip():
        return str(node).strip()
    return None


def allowed_user_id() -> str:
    """Telegram user id permitted to approve.

    No baked-in default: publishing the sole authorised chat id in source is
    audit S-4, and a silent fallback would let a misconfigured deployment
    authorise the wrong account.
    """
    v = _config_value("OPENCLAW_ALLOWED_USER_ID", "telegram", "allowed_user_id")
    if not v:
        raise ConfigError(
            "OPENCLAW_ALLOWED_USER_ID is not set and config/juma.json has no "
            "telegram.allowed_user_id; refusing to fall back to a baked-in id"
        )
    return v


def gateway_url() -> str:
    return _config_value("OPENCLAW_GATEWAY_URL", "openclaw", "gateway_url") \
        or "http://127.0.0.1:18789"


def gateway_token() -> str:
    """Shared secret for the OpenClaw gateway (gateway.auth.mode: token)."""
    v = _config_value("OPENCLAW_GATEWAY_TOKEN", "openclaw", "gateway_token")
    if not v:
        raise ConfigError(
            "OPENCLAW_GATEWAY_TOKEN is not set; the OpenClaw gateway runs with "
            "gateway.auth.mode=token and will reject unauthenticated requests"
        )
    return v

SAFE_POLICIES = {
    # Actions explicitly designated safe without approval
    "read", "list", "get", "search", "query", "explain", "analyze"
}

AUTHORITY_MAP = {
    # Research agent
    "research": "READ",
    # Projects agent
    "list_spaces": "READ",
    "search_tasks": "READ",
    "get_task": "READ",
    "list_lists": "READ",
    "list_folders": "READ",
    # Writing to a third party's SaaS account is an external action, whatever
    # it is internal to. Reclassified per audit F-1.
    "create_task": "EXTERNAL_ACTION",
    "update_task": "EXTERNAL_ACTION",
    # Coding agent
    "explain": "READ",
    "review": "READ",
    "generate": "INTERNAL_WRITE",
    "debug": "INTERNAL_WRITE",
    "refactor": "INTERNAL_WRITE",
    "test": "INTERNAL_WRITE",
}


def classify(action: str) -> str:
    """Classify an action, failing closed.

    Only explicitly registered actions get a reduced authority. Anything else
    -- including an empty action and any action added in future -- is
    EXTERNAL_ACTION and therefore requires approval. The previous substring
    heuristic defaulted to READ, so 'wire_transfer' and 'pay_invoice' ran
    ungated (audit F-1).
    """
    if not action:
        return "EXTERNAL_ACTION"
    a = action.lower().strip()
    if a in SAFE_POLICIES:
        return "READ"
    return AUTHORITY_MAP.get(a, "EXTERNAL_ACTION")


def _request_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.request.json"


def _decision_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.decision.json"


# Public aliases: every module must derive approval-state paths from here so
# the AGENTIC_STATE_DIR knob and the file-naming convention have one owner.
def request_path(request_id: str) -> Path:
    return _request_path(request_id)


def decision_path(request_id: str) -> Path:
    return _decision_path(request_id)


def request_approval(proposal: dict, enquiry: str | None = None) -> dict:
    """Create an approval request and return the request record.

    ``enquiry`` is optional so the flagship workflow can record the originating
    client message alongside the proposal and emit the matching evidence file.
    When it is omitted the behaviour is the plain approval-boundary request.
    Writes are idempotent: an existing request or evidence file for the same
    id is never overwritten, so a retry cannot clobber a pending decision.
    """
    APPROVAL_DIR.mkdir(exist_ok=True)
    request_id = (
        proposal.get("request_id")
        or proposal.get("proposal_id")
        or str(uuid.uuid4())
    )
    record = {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proposal": proposal,
        "status": "pending",
        "expires_at": datetime.now(timezone.utc).isoformat(),  # simplified
    }
    if enquiry is not None:
        record["enquiry"] = enquiry
        EVIDENCE_DIR.mkdir(exist_ok=True)
        evidence_path = EVIDENCE_DIR / f"{request_id}-proposal.json"
        if not evidence_path.exists():
            evidence_path.write_text(json.dumps(proposal, indent=2))
    request_path = _request_path(request_id)
    if not request_path.exists():
        request_path.write_text(json.dumps(record, indent=2))
    return record


def wait_for_decision(request_id: str, timeout: int = 300) -> dict:
    """
    Poll for approval decision.
    
    Returns decision dict with keys:
      - approved: bool
      - approver: str | None
      - timestamp: str | None
      - reason: str | None
    """
    deadline = time.time() + timeout
    decision_file = _decision_path(request_id)
    while time.time() < deadline:
        if decision_file.exists():
            try:
                return json.loads(decision_file.read_text())
            except Exception:
                pass
        time.sleep(0.5)
    return {"approved": False, "reason": "timeout", "timestamp": None, "approver": None}


def record_decision(request_id: str, approved: bool, approver: str = None, reason: str = None, overwrite: bool = False, channel: str = None):
    path = _decision_path(request_id)
    if path.exists() and not overwrite:
        try:
            existing = json.loads(path.read_text())
            return existing
        except Exception:
            pass
    decision = {
        "request_id": request_id,
        "approved": approved,
        "approver": approver or "human",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason or ("approved" if approved else "rejected"),
    }
    if channel:
        decision["channel"] = channel
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision, indent=2))
    return decision


def is_approved(request_id: str) -> bool:
    decision_file = _decision_path(request_id)
    if not decision_file.exists():
        return False
    try:
        decision = json.loads(decision_file.read_text())
        return decision.get("approved") is True
    except Exception:
        return False


def enforce(step: dict, task_context: dict = None) -> dict:
    """
    Enforce approval boundary for a step.
    
    Returns:
      - {"allowed": True, "authority": "READ|INTERNAL_WRITE", "executed": True}
      - {"allowed": True, "authority": "EXTERNAL_ACTION", "executed": False, "request_id": ...}
      - {"allowed": False, "reason": ..., "request_id": ...}
    """
    action = step.get("action") or step.get("step") or "read"
    authority = classify(action)
    
    if authority == "READ":
        return {"allowed": True, "authority": authority, "executed": True}
    
    if authority == "INTERNAL_WRITE":
        return {"allowed": True, "authority": authority, "executed": True}
    
    # EXTERNAL ACTION
    proposal = {
        "request_id": step.get("request_id"),
        "task": (task_context or {}).get("task", ""),
        "action": action,
        "agent": step.get("agent"),
        "description": step.get("description") or f"External action: {action} via {step.get('agent')}",
        "authority": authority,
        "payload": step.get("payload"),
    }
    record = request_approval(proposal)
    return {
        "allowed": True,
        "authority": authority,
        "executed": False,
        "request_id": record["request_id"],
        "status": "awaiting_approval",
        "proposal": proposal,
    }


def resume_if_approved(request_id: str) -> dict:
    """
    Check if an external action was approved and is ready for execution.
    
    Returns:
      - {"approved": True, "executed": True}
      - {"approved": False, "reason": "rejected|timeout|pending"}
    """
    if not request_id:
        return {"approved": False, "reason": "missing_request_id"}
    decision_file = _decision_path(request_id)
    if not decision_file.exists():
        return {"approved": False, "reason": "pending"}
    try:
        decision = json.loads(decision_file.read_text())
        if decision.get("approved") is True:
            return {"approved": True, "executed": True, "decision": decision}
        return {"approved": False, "reason": decision.get("reason", "rejected"), "decision": decision}
    except Exception as e:
        return {"approved": False, "reason": f"invalid_decision: {e}"}


def cleanup(request_id: str):
    """Remove every artifact for a request: request, decision, and evidence.

    The evidence file was previously left behind, so callers that expected a
    clean slate -- including every test suite's teardown -- silently
    accumulated proposal JSON.
    """
    for p in [
        _request_path(request_id),
        _decision_path(request_id),
        EVIDENCE_DIR / f"{request_id}-proposal.json",
    ]:
        if p.exists():
            p.unlink()
